from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import requests
import xml.etree.ElementTree as ET
import time
import re

app = Flask(__name__)
CORS(app)

EDGAR_HEADERS = {
    'User-Agent': 'FundCorrelationTool contact@fundcorrelation.app',
    'Accept-Encoding': 'gzip, deflate'
}

_company_tickers = None
_holdings_cache = {}
CACHE_TTL = 60 * 60 * 6

# Goldman Sachs ETF Trust: trust CIK 1479026, filing agent CIK 940400
KNOWN_TRUST_ETFS = {
    'GPIX': {'trust_cik': '1479026', 'filer_cik': '940400', 'series_id': 'S000081511'},
    'GPIQ': {'trust_cik': '1479026', 'filer_cik': '940400', 'series_id': 'S000081510'},
}


# ── PRICES ───────────────────────────────────────────────────────────────────

@app.route('/api/prices')
def get_prices():
    ticker = request.args.get('ticker', '').upper().strip()
    months = int(request.args.get('months', 36))
    if not ticker:
        return jsonify({'error': 'No ticker provided'}), 400
    try:
        period_map = {12: '1y', 24: '2y', 36: '3y', 60: '5y', 120: '10y'}
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period_map.get(months, '3y'), interval='1mo')
        if hist.empty:
            return jsonify({'error': f'No data found for {ticker}'}), 404
        prices = [{'date': d.strftime('%Y-%m-%d'), 'price': round(float(r['Close']), 4)}
                  for d, r in hist.iterrows()]
        return jsonify({'ticker': ticker, 'prices': prices})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── ACCESSION HELPERS ─────────────────────────────────────────────────────────

def nd(acc):
    """nodash: 000094040026028192"""
    return acc.strip().replace('-', '').zfill(18)

def dd(acc):
    """dashed: 0000940400-26-028192"""
    s = nd(acc)
    return f'{s[:10]}-{s[10:12]}-{s[12:]}'


# ── EDGAR HELPERS ─────────────────────────────────────────────────────────────

def get_company_tickers():
    global _company_tickers
    if _company_tickers is None:
        r = requests.get('https://www.sec.gov/files/company_tickers.json',
                         headers=EDGAR_HEADERS, timeout=20)
        r.raise_for_status()
        _company_tickers = r.json()
    return _company_tickers


def find_cik(ticker):
    for val in get_company_tickers().values():
        if val.get('ticker', '').upper() == ticker.upper():
            return str(val['cik_str'])
    return None


def get_nport_filings(cik):
    """All NPORT-P filings for a CIK, handling pagination."""
    cik_padded = str(cik).zfill(10)
    r = requests.get(f'https://data.sec.gov/submissions/CIK{cik_padded}.json',
                     headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    forms, accs, dates = [], [], []
    recent = data.get('filings', {}).get('recent', {})
    forms += recent.get('form', [])
    accs  += recent.get('accessionNumber', [])
    dates += recent.get('filingDate', [])

    for f in data.get('filings', {}).get('files', [])[:3]:
        fname = f.get('name', '')
        if not fname:
            continue
        try:
            time.sleep(0.1)
            r2 = requests.get(f'https://data.sec.gov/submissions/{fname}',
                              headers=EDGAR_HEADERS, timeout=15)
            if r2.ok:
                p = r2.json()
                forms += p.get('form', [])
                accs  += p.get('accessionNumber', [])
                dates += p.get('filingDate', [])
        except Exception:
            pass

    return [{'accession': accs[i], 'date': dates[i]}
            for i, f in enumerate(forms) if f in ('NPORT-P', 'NPORT-P/A')]


def get_index_page(filer_cik, acc):
    """
    Fetch the full index page for a filing.
    Tries both .htm and .html extensions.
    Returns full text content or None.
    """
    nodash_acc = nd(acc)
    dashed_acc = dd(acc)
    cik_int = int(filer_cik)

    for ext in ['.htm', '.html']:
        url = (f'https://www.sec.gov/Archives/edgar/data/{cik_int}/'
               f'{nodash_acc}/{dashed_acc}-index{ext}')
        try:
            time.sleep(0.3)
            r = requests.get(url, headers=EDGAR_HEADERS, timeout=12)
            if r.ok:
                return r.text
            elif r.status_code == 429:
                time.sleep(3)
        except Exception:
            pass
    return None


def extract_xml_url(index_content, trust_cik):
    """
    Extract the raw XML URL from an index page.
    Prefers files under trust_cik, skips xsl viewer versions.
    """
    all_xml = re.findall(r'href="(/Archives/edgar/data/[^"]*\.xml)"',
                         index_content, re.IGNORECASE)
    # Skip xsl viewer, prefer trust_cik path
    raw = [x for x in all_xml if 'xsl' not in x.lower()]
    trust = [x for x in raw if f'/data/{trust_cik}/' in x or f'/data/0*{trust_cik}/' in x]
    chosen = trust[0] if trust else (raw[0] if raw else None)
    return ('https://www.sec.gov' + chosen) if chosen else None


def index_matches_series(content, series_id, ticker):
    """
    Check if an index page belongs to a specific series.
    From EDGAR index pages we know the series table format:
      Series S000081511   Goldman Sachs S&P 500 Premium Income ETF
      ...Ticker Symbol... GPIX
    So we can search for series_id or ticker in the full content.
    """
    return series_id in content or ticker.upper() in content


def find_filing_for_series(trust_cik, filer_cik, ticker, series_id, max_scan=60):
    """
    Scan NPORT-P filings for a trust to find the one for a specific series.
    Fetches each filing's index page and checks for series_id or ticker.
    """
    filings = get_nport_filings(trust_cik)

    for filing in filings[:max_scan]:
        content = get_index_page(filer_cik, filing['accession'])
        if content is None:
            continue
        if index_matches_series(content, series_id, ticker):
            xml_url = extract_xml_url(content, trust_cik)
            return {**filing, 'xml_url': xml_url}

    return None


def fetch_xml(trust_cik, acc, xml_url=None):
    """Download raw NPORT-P XML. Uses xml_url if provided, else finds it from index."""
    if xml_url:
        time.sleep(0.2)
        r = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=45)
        r.raise_for_status()
        return r.content

    nodash_acc = nd(acc)
    dashed_acc = dd(acc)
    cik_int = int(trust_cik)
    base = f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash_acc}'

    for ext in ['.htm', '.html']:
        index_url = f'{base}/{dashed_acc}-index{ext}'
        try:
            r = requests.get(index_url, headers=EDGAR_HEADERS, timeout=12)
            if r.ok:
                url = extract_xml_url(r.text, trust_cik)
                if url:
                    time.sleep(0.2)
                    rx = requests.get(url, headers=EDGAR_HEADERS, timeout=45)
                    rx.raise_for_status()
                    return rx.content
            break
        except Exception:
            pass

    # Final fallback
    time.sleep(0.2)
    r = requests.get(f'{base}/primary_doc.xml', headers=EDGAR_HEADERS, timeout=45)
    r.raise_for_status()
    return r.content


# ── XML PARSING ───────────────────────────────────────────────────────────────

def parse_holdings(xml_content):
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    NS = ['http://www.sec.gov/edgar/nport', 'http://www.sec.gov/edgar/nportfund', '']

    def find_all(node, tag):
        for ns in NS:
            res = node.findall(f'.//{{{ns}}}{tag}') if ns else node.findall(f'.//{tag}')
            if res:
                return res
        return []

    def find_text(el, tag):
        for ns in NS:
            child = el.find(f'{{{ns}}}{tag}') if ns else el.find(tag)
            if child is not None and child.text:
                return child.text.strip()
        return ''

    total_assets = 0
    for tag in ['netAssets', 'totAssets']:
        els = find_all(root, tag)
        if els and els[0].text:
            try: total_assets = float(els[0].text); break
            except: pass

    holdings = []
    for inv in find_all(root, 'invstOrSec'):
        name = find_text(inv, 'name')
        if not name:
            continue
        cusip  = find_text(inv, 'cusip')
        ticker = find_text(inv, 'ticker')
        weight = 0.0
        pct    = find_text(inv, 'pctVal')
        val    = find_text(inv, 'valUSD')
        if pct:
            try: weight = float(pct)
            except: pass
        elif val and total_assets > 0:
            try: weight = (float(val) / total_assets) * 100
            except: pass
        holdings.append({'name': name, 'cusip': cusip, 'ticker': ticker,
                         'weight': round(weight, 4)})

    holdings.sort(key=lambda x: x['weight'], reverse=True)
    return holdings


# ── OVERLAP ───────────────────────────────────────────────────────────────────

def normalize_key(h):
    cusip = h.get('cusip', '').strip()
    if cusip and len(cusip) >= 6 and cusip.upper() not in ('N/A', 'NA', '000000000', ''):
        return ('cusip', cusip)
    t = h.get('ticker', '').strip().upper()
    if t and t not in ('N/A', 'NA', ''):
        return ('ticker', t)
    name = h.get('name', '').upper().strip()
    for s in [' COMMON STOCK', ' COM', ' INC', ' CORP', ' LTD',
              ' CLASS A', ' CLASS B', ' CL A', ' CL B']:
        name = name.replace(s, '')
    return ('name', name.strip())


def calculate_overlap(a, b):
    ma = {normalize_key(h): h for h in a}
    mb = {normalize_key(h): h for h in b}
    shared = []
    for key in set(ma) & set(mb):
        ha, hb = ma[key], mb[key]
        shared.append({
            'name': ha['name'],
            'ticker': ha.get('ticker') or hb.get('ticker') or '',
            'cusip': ha.get('cusip') or hb.get('cusip') or '',
            'weight_a': ha['weight'], 'weight_b': hb['weight'],
            'avg_weight': round((ha['weight'] + hb['weight']) / 2, 4)
        })
    shared.sort(key=lambda x: x['avg_weight'], reverse=True)
    ov_a = sum(h['weight_a'] for h in shared)
    ov_b = sum(h['weight_b'] for h in shared)
    return {
        'shared_count': len(shared),
        'overlap_pct_a': round(ov_a, 2),
        'overlap_pct_b': round(ov_b, 2),
        'overlap_pct_avg': round((ov_a + ov_b) / 2, 2),
        'shared_holdings': shared[:50]
    }


# ── MASTER HOLDINGS FETCHER ───────────────────────────────────────────────────

def get_fund_holdings(ticker):
    ticker_upper = ticker.upper()
    now = time.time()

    if ticker_upper in _holdings_cache:
        c = _holdings_cache[ticker_upper]
        if now - c['ts'] < CACHE_TTL:
            return c['holdings'], c['date']

    # Path 1: Known trust ETFs
    if ticker_upper in KNOWN_TRUST_ETFS:
        info = KNOWN_TRUST_ETFS[ticker_upper]
        filing = find_filing_for_series(
            info['trust_cik'], info['filer_cik'],
            ticker_upper, info['series_id']
        )
        if not filing:
            return None, None
        xml = fetch_xml(info['trust_cik'], filing['accession'],
                        xml_url=filing.get('xml_url'))
        holdings = parse_holdings(xml)
        if holdings:
            _holdings_cache[ticker_upper] = {'holdings': holdings,
                                              'date': filing['date'], 'ts': now}
        return holdings, filing['date']

    # Path 2: Direct filers (CEFs, independent ETFs)
    cik = find_cik(ticker_upper)
    if not cik:
        return None, None
    filings = get_nport_filings(cik)
    if not filings:
        return None, None
    filing = filings[0]
    time.sleep(0.2)
    xml = fetch_xml(cik, filing['accession'])
    holdings = parse_holdings(xml)
    if holdings:
        _holdings_cache[ticker_upper] = {'holdings': holdings,
                                          'date': filing['date'], 'ts': now}
    return holdings, filing['date']


# ── DIAGNOSTIC ────────────────────────────────────────────────────────────────

@app.route('/api/debug-gpix')
def debug_gpix():
    import traceback
    out = {}
    try:
        info = KNOWN_TRUST_ETFS['GPIX']
        filings = get_nport_filings(info['trust_cik'])
        out['total_nport_filings'] = len(filings)
        out['scanning'] = []

        # Scan filings one by one until we find GPIX
        for i, filing in enumerate(filings[:20]):
            acc = filing['accession']
            content = get_index_page(info['filer_cik'], acc)
            has_gpix = 'GPIX' in (content or '')
            has_sid  = info['series_id'] in (content or '')
            ticker_found = None
            # Extract whichever ticker is in this filing
            if content:
                m = re.search(r'<td>([A-Z]{2,6})</td>\s*</tr>\s*</table>', content)
                if m:
                    ticker_found = m.group(1)
            entry = {
                'index': i,
                'accession': acc,
                'date': filing['date'],
                'fetched': content is not None,
                'has_GPIX': has_gpix,
                'has_series_id': has_sid,
                'ticker_in_filing': ticker_found
            }
            out['scanning'].append(entry)
            if has_gpix or has_sid:
                out['FOUND_AT_INDEX'] = i
                out['FOUND_ACCESSION'] = acc
                out['xml_url'] = extract_xml_url(content, info['trust_cik'])
                break

    except Exception as e:
        out['error'] = str(e)
        out['traceback'] = traceback.format_exc()
    return jsonify(out)


# ── HOLDINGS OVERLAP ENDPOINT ─────────────────────────────────────────────────

@app.route('/api/holdings-overlap')
def holdings_overlap():
    ticker_a = request.args.get('tickerA', '').upper().strip()
    ticker_b = request.args.get('tickerB', '').upper().strip()
    if not ticker_a or not ticker_b:
        return jsonify({'error': 'Both tickerA and tickerB are required'}), 400
    try:
        holdings_a, date_a = get_fund_holdings(ticker_a)
        if not holdings_a:
            return jsonify({'error': f'Could not retrieve holdings for {ticker_a}.'}), 404
        time.sleep(0.3)
        holdings_b, date_b = get_fund_holdings(ticker_b)
        if not holdings_b:
            return jsonify({'error': f'Could not retrieve holdings for {ticker_b}.'}), 404
        overlap = calculate_overlap(holdings_a, holdings_b)
        return jsonify({
            'ticker_a': ticker_a, 'ticker_b': ticker_b,
            'filing_date_a': date_a, 'filing_date_b': date_b,
            'total_holdings_a': len(holdings_a),
            'total_holdings_b': len(holdings_b),
            **overlap
        })
    except requests.exceptions.Timeout:
        return jsonify({'error': 'SEC EDGAR timed out. Please try again.'}), 504
    except requests.exceptions.HTTPError as e:
        code = 429 if '429' in str(e) else 500
        return jsonify({'error': f'HTTP error: {str(e)}'}), code
    except Exception as e:
        return jsonify({'error': f'Error: {str(e)}'}), 500


@app.route('/')
def index():
    return jsonify({'status': 'Fund Correlation API is running', 'version': '11.1'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
