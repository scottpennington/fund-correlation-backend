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

# ─────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────
_company_tickers = None
_holdings_cache = {}
CACHE_TTL = 60 * 60 * 6  # 6 hours

# Known ETF trust series: ticker -> {cik, series_id}
# cik = trust's CIK, series_id = this fund's series within the trust
KNOWN_SERIES = {
    'GPIX': {'cik': '1479026', 'series_id': 'S000081511'},
    'GPIQ': {'cik': '1479026', 'series_id': 'S000081510'},
}


# ─────────────────────────────────────────
# PRICES ENDPOINT
# ─────────────────────────────────────────

@app.route('/api/prices')
def get_prices():
    ticker = request.args.get('ticker', '').upper().strip()
    months = int(request.args.get('months', 36))
    if not ticker:
        return jsonify({'error': 'No ticker provided'}), 400
    try:
        period_map = {12: '1y', 24: '2y', 36: '3y', 60: '5y', 120: '10y'}
        period = period_map.get(months, '3y')
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period, interval='1mo')
        if hist.empty:
            return jsonify({'error': f'No data found for {ticker}'}), 404
        prices = [{'date': d.strftime('%Y-%m-%d'), 'price': round(float(r['Close']), 4)}
                  for d, r in hist.iterrows()]
        return jsonify({'ticker': ticker, 'prices': prices})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# ACCESSION UTILITIES
# ─────────────────────────────────────────

def to_nodash(accession):
    """Return 18-char nodash accession: 000175272425042696"""
    return accession.strip().replace('-', '').zfill(18)


def to_dashed(accession):
    """Return dashed accession: 0001752724-25-042696"""
    s = to_nodash(accession)
    return f'{s[:10]}-{s[10:12]}-{s[12:]}'


# ─────────────────────────────────────────
# EDGAR HELPERS
# ─────────────────────────────────────────

def get_company_tickers():
    global _company_tickers
    if _company_tickers is not None:
        return _company_tickers
    r = requests.get('https://www.sec.gov/files/company_tickers.json',
                     headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    _company_tickers = r.json()
    return _company_tickers


def find_cik_for_ticker(ticker):
    companies = get_company_tickers()
    for val in companies.values():
        if val.get('ticker', '').upper() == ticker.upper():
            return str(val['cik_str'])
    return None


def get_submissions(cik):
    """Fetch the submissions JSON for a CIK from data.sec.gov."""
    cik_padded = str(cik).zfill(10)
    url = f'https://data.sec.gov/submissions/CIK{cik_padded}.json'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def find_nport_for_series(cik, series_id):
    """
    Search the submissions JSON for a trust CIK to find the most recent
    NPORT-P filing that belongs to a specific series ID.

    The SEC submissions JSON has a 'filings.recent' block with parallel arrays.
    Each filing also has a 'seriesId' field (if set) in an older format,
    OR we need to check the filing index page for series info.

    Strategy: Get all recent NPORT-P accessions for the trust, then check
    each index page for the matching series ID. Cache the result.
    """
    data = get_submissions(cik)
    filings = data.get('filings', {}).get('recent', {})
    forms = filings.get('form', [])
    accessions = filings.get('accessionNumber', [])
    dates = filings.get('filingDate', [])

    # Collect all NPORT-P filings for this trust
    nport_filings = []
    for i, form in enumerate(forms):
        if form in ('NPORT-P', 'NPORT-P/A'):
            nport_filings.append({
                'accession': accessions[i],
                'date': dates[i]
            })

    # Check each filing's index page for the series ID
    # Start with the most recent and work backwards
    for filing in nport_filings[:20]:  # Check last 20 NPORT-P filings
        acc_nodash = to_nodash(filing['accession'])
        acc_dashed = to_dashed(filing['accession'])
        index_url = (
            f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/'
            f'{acc_nodash}/{acc_dashed}-index.htm'
        )
        try:
            time.sleep(0.1)
            r = requests.get(index_url, headers=EDGAR_HEADERS, timeout=15)
            if r.ok and series_id in r.text:
                return {
                    'cik': str(cik),
                    'accession': filing['accession'],
                    'date': filing['date']
                }
        except Exception:
            continue

    return None


def get_latest_nport_for_cik(cik):
    """Get most recent NPORT-P for a direct filer (CEF, independent ETF)."""
    data = get_submissions(cik)
    filings = data.get('filings', {}).get('recent', {})
    forms = filings.get('form', [])
    accessions = filings.get('accessionNumber', [])
    dates = filings.get('filingDate', [])
    for i, form in enumerate(forms):
        if form in ('NPORT-P', 'NPORT-P/A'):
            return {'cik': str(cik), 'accession': accessions[i], 'date': dates[i]}
    return None


def fetch_nport_xml(cik, accession):
    """
    Download primary_doc.xml from an NPORT-P filing.
    URL pattern: /Archives/edgar/data/{CIK_INT}/{NODASH}/{DASHED}-index.htm
    XML is at: /Archives/edgar/data/{CIK_INT}/{NODASH}/primary_doc.xml
    """
    cik_int = int(cik)
    nodash = to_nodash(accession)
    dashed = to_dashed(accession)
    base = f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}'

    xml_url = None

    # Try index page first to find the exact XML filename
    index_url = f'{base}/{dashed}-index.htm'
    try:
        r = requests.get(index_url, headers=EDGAR_HEADERS, timeout=15)
        if r.ok:
            # Find href to primary_doc.xml
            matches = re.findall(
                r'href="(/Archives/edgar/data/[^"]*primary_doc\.xml)"',
                r.text, re.IGNORECASE
            )
            if matches:
                xml_url = 'https://www.sec.gov' + matches[0]
            else:
                # Find any .xml file
                matches = re.findall(
                    r'href="(/Archives/edgar/data/[^"]*\.xml)"',
                    r.text, re.IGNORECASE
                )
                if matches:
                    xml_url = 'https://www.sec.gov' + matches[0]
    except Exception:
        pass

    if not xml_url:
        xml_url = f'{base}/primary_doc.xml'

    time.sleep(0.15)
    r = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=45)
    r.raise_for_status()
    return r.content


# ─────────────────────────────────────────
# XML PARSING
# ─────────────────────────────────────────

def parse_nport_xml(xml_content):
    """Parse holdings from N-PORT XML."""
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

    invstOrSecs = find_all(root, 'invstOrSec')
    total_assets = 0
    for tag in ['netAssets', 'totAssets']:
        els = find_all(root, tag)
        if els and els[0].text:
            try:
                total_assets = float(els[0].text)
                break
            except Exception:
                pass

    holdings = []
    for inv in invstOrSecs:
        name = find_text(inv, 'name')
        if not name:
            continue
        cusip = find_text(inv, 'cusip')
        ticker = find_text(inv, 'ticker')
        pct_val = find_text(inv, 'pctVal')
        val_usd = find_text(inv, 'valUSD')
        weight = 0.0
        if pct_val:
            try:
                weight = float(pct_val)
            except Exception:
                pass
        elif val_usd and total_assets > 0:
            try:
                weight = (float(val_usd) / total_assets) * 100
            except Exception:
                pass
        holdings.append({
            'name': name, 'cusip': cusip,
            'ticker': ticker, 'weight': round(weight, 4)
        })

    holdings.sort(key=lambda x: x['weight'], reverse=True)
    return holdings


# ─────────────────────────────────────────
# OVERLAP CALCULATION
# ─────────────────────────────────────────

def normalize_key(h):
    cusip = h.get('cusip', '').strip()
    if cusip and len(cusip) >= 6 and cusip.upper() not in ('N/A', 'NA', '000000000', ''):
        return ('cusip', cusip)
    ticker = h.get('ticker', '').strip().upper()
    if ticker and ticker not in ('N/A', 'NA', ''):
        return ('ticker', ticker)
    name = h.get('name', '').upper().strip()
    for s in [' COMMON STOCK', ' COM', ' INC', ' CORP', ' LTD',
              ' CLASS A', ' CLASS B', ' CL A', ' CL B', ' CLASS C']:
        name = name.replace(s, '')
    return ('name', name.strip())


def calculate_overlap(holdings_a, holdings_b):
    map_a = {normalize_key(h): h for h in holdings_a}
    map_b = {normalize_key(h): h for h in holdings_b}
    common = set(map_a.keys()) & set(map_b.keys())
    shared = []
    for key in common:
        ha, hb = map_a[key], map_b[key]
        shared.append({
            'name': ha['name'],
            'ticker': ha.get('ticker') or hb.get('ticker') or '',
            'cusip': ha.get('cusip') or hb.get('cusip') or '',
            'weight_a': ha['weight'],
            'weight_b': hb['weight'],
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


# ─────────────────────────────────────────
# MASTER HOLDINGS FETCHER
# ─────────────────────────────────────────

def get_fund_holdings(ticker):
    """Returns (holdings_list, filing_date) for any ticker."""
    ticker_upper = ticker.upper()
    now = time.time()

    if ticker_upper in _holdings_cache:
        c = _holdings_cache[ticker_upper]
        if now - c['ts'] < CACHE_TTL:
            return c['holdings'], c['date']

    # ── Path 1: Known trust ETFs — scan submissions for matching series ──
    if ticker_upper in KNOWN_SERIES:
        info = KNOWN_SERIES[ticker_upper]
        cik = info['cik']
        series_id = info['series_id']

        time.sleep(0.2)
        filing = find_nport_for_series(cik, series_id)
        if not filing:
            return None, None

        time.sleep(0.2)
        xml = fetch_nport_xml(filing['cik'], filing['accession'])
        holdings = parse_nport_xml(xml)
        if holdings:
            _holdings_cache[ticker_upper] = {
                'holdings': holdings, 'date': filing['date'], 'ts': now
            }
        return holdings, filing['date']

    # ── Path 2: Direct filers (CEFs, independent ETFs) ──
    cik = find_cik_for_ticker(ticker_upper)
    if not cik:
        return None, None

    time.sleep(0.2)
    filing = get_latest_nport_for_cik(cik)
    if not filing:
        return None, None

    time.sleep(0.2)
    xml = fetch_nport_xml(filing['cik'], filing['accession'])
    holdings = parse_nport_xml(xml)
    if holdings:
        _holdings_cache[ticker_upper] = {
            'holdings': holdings, 'date': filing['date'], 'ts': now
        }
    return holdings, filing['date']


# ─────────────────────────────────────────
# HOLDINGS OVERLAP ENDPOINT
# ─────────────────────────────────────────

@app.route('/api/holdings-overlap')
def holdings_overlap():
    ticker_a = request.args.get('tickerA', '').upper().strip()
    ticker_b = request.args.get('tickerB', '').upper().strip()
    if not ticker_a or not ticker_b:
        return jsonify({'error': 'Both tickerA and tickerB are required'}), 400
    try:
        holdings_a, date_a = get_fund_holdings(ticker_a)
        if not holdings_a:
            return jsonify({'error': f'Could not retrieve holdings for {ticker_a}. '
                                     f'It may not file N-PORT reports with the SEC.'}), 404
        time.sleep(0.3)
        holdings_b, date_b = get_fund_holdings(ticker_b)
        if not holdings_b:
            return jsonify({'error': f'Could not retrieve holdings for {ticker_b}. '
                                     f'It may not file N-PORT reports with the SEC.'}), 404
        overlap = calculate_overlap(holdings_a, holdings_b)
        return jsonify({
            'ticker_a': ticker_a,
            'ticker_b': ticker_b,
            'filing_date_a': date_a,
            'filing_date_b': date_b,
            'total_holdings_a': len(holdings_a),
            'total_holdings_b': len(holdings_b),
            **overlap
        })
    except requests.exceptions.Timeout:
        return jsonify({'error': 'SEC EDGAR timed out. Please try again.'}), 504
    except requests.exceptions.HTTPError as e:
        if '429' in str(e):
            return jsonify({'error': 'SEC EDGAR rate limit hit. Please wait 30 seconds and try again.'}), 429
        return jsonify({'error': f'HTTP error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Error: {str(e)}'}), 500


@app.route('/')
def index():
    return jsonify({'status': 'Fund Correlation API is running', 'version': '7.0'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
