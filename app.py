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

# Hardcoded known filings for trust ETFs.
# 'cik' is the FILER CIK — the entity that actually submits filings to EDGAR.
# This is often a filing agent, NOT the trust's own CIK.
# Goldman Sachs ETF Trust (trust CIK 1479026) uses filing agent CIK 0000940400.
# We discovered this via the /api/debug-submissions diagnostic endpoint.
HARDCODED_FILINGS = {
    'GPIX': {
        'cik': '940400',         # Filing agent CIK (not trust CIK)
        'series_id': 'S000081511',
        'class_id': 'C000244421',
    },
    'GPIQ': {
        'cik': '940400',
        'series_id': 'S000081510',
        'class_id': 'C000244420',
    },
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
# DIAGNOSTIC ENDPOINT
# ─────────────────────────────────────────

@app.route('/api/debug-index')
def debug_index():
    """Fetch raw text of an NPORT-P filing index page to inspect its contents."""
    accession = request.args.get('accession', '0000940400-26-028192')
    nodash = accession.replace('-', '').zfill(18)
    dashed = f'{nodash[:10]}-{nodash[10:12]}-{nodash[12:]}'
    filer_cik = int(nodash[:10])
    results = {'accession': accession, 'nodash': nodash, 'filer_cik': filer_cik}
    for ext in ['.htm', '.html']:
        url = f'https://www.sec.gov/Archives/edgar/data/{filer_cik}/{nodash}/{dashed}-index{ext}'
        results[f'url{ext}'] = url
        try:
            r = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
            results[f'status{ext}'] = r.status_code
            if r.ok:
                results['content'] = r.text[:3000]
                results['found'] = True
                return jsonify(results)
        except Exception as e:
            results[f'error{ext}'] = str(e)
    results['found'] = False
    return jsonify(results)


@app.route('/api/debug-submissions')
def debug_submissions():
    """
    Diagnostic endpoint: returns the raw list of recent NPORT-P filings
    for a given CIK, so we can see what accessions exist and debug lookups.
    """
    cik = request.args.get('cik', '940400')
    cik_padded = str(cik).zfill(10)
    try:
        url = f'https://data.sec.gov/submissions/CIK{cik_padded}.json'
        r = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        filings = data.get('filings', {}).get('recent', {})
        forms = filings.get('form', [])
        accessions = filings.get('accessionNumber', [])
        dates = filings.get('filingDate', [])

        nports = []
        for i, form in enumerate(forms):
            if form in ('NPORT-P', 'NPORT-P/A'):
                nports.append({
                    'form': form,
                    'accession': accessions[i],
                    'date': dates[i]
                })

        return jsonify({
            'cik': cik_padded,
            'name': data.get('name', ''),
            'total_recent_filings': len(forms),
            'nport_filings': nports[:10],  # Show first 10
            'has_more_pages': bool(data.get('filings', {}).get('files', []))
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# ACCESSION UTILITIES
# ─────────────────────────────────────────

def to_nodash(accession):
    return accession.strip().replace('-', '').zfill(18)


def to_dashed(accession):
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


def get_submissions_filings(cik):
    """
    Fetch ALL NPORT-P filings for a CIK, handling pagination.
    The SEC submissions API returns 'recent' for the latest batch,
    and 'files' contains paths to older batches.
    """
    cik_padded = str(cik).zfill(10)
    url = f'https://data.sec.gov/submissions/CIK{cik_padded}.json'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    all_accessions = []
    all_dates = []
    all_forms = []

    # Add recent filings
    recent = data.get('filings', {}).get('recent', {})
    all_forms.extend(recent.get('form', []))
    all_accessions.extend(recent.get('accessionNumber', []))
    all_dates.extend(recent.get('filingDate', []))

    # Handle pagination — older filings in separate files
    older_files = data.get('filings', {}).get('files', [])
    for f in older_files[:3]:  # Check up to 3 older pages
        fname = f.get('name', '')
        if not fname:
            continue
        try:
            time.sleep(0.1)
            r2 = requests.get(
                f'https://data.sec.gov/submissions/{fname}',
                headers=EDGAR_HEADERS, timeout=15
            )
            if r2.ok:
                page = r2.json()
                all_forms.extend(page.get('form', []))
                all_accessions.extend(page.get('accessionNumber', []))
                all_dates.extend(page.get('filingDate', []))
        except Exception:
            pass

    # Return only NPORT-P filings
    nports = []
    for i, form in enumerate(all_forms):
        if form in ('NPORT-P', 'NPORT-P/A'):
            nports.append({
                'accession': all_accessions[i],
                'date': all_dates[i]
            })
    return nports


def find_nport_for_series(cik, series_id, class_id=None):
    """
    Find the most recent NPORT-P filing for a specific series
    by checking each filing's index page for the series_id.
    """
    nports = get_submissions_filings(cik)

    for filing in nports[:30]:  # Check up to 30 most recent NPORT-Ps
        acc = filing['accession']
        nodash = to_nodash(acc)
        dashed = to_dashed(acc)
        cik_int = int(cik)

        # Try both .htm and .html index URL variants
        for ext in ['.htm', '.html']:
            # Use the filer CIK (filing agent) for the URL path
        filer_cik_int = int(to_nodash(acc)[:10])
        index_url = (
                f'https://www.sec.gov/Archives/edgar/data/{filer_cik_int}/'
                f'{nodash}/{dashed}-index{ext}'
            )
            try:
                time.sleep(0.12)
                r = requests.get(index_url, headers=EDGAR_HEADERS, timeout=12)
                if r.ok:
                    content = r.text
                    # Check if this filing belongs to our series
                    if series_id in content:
                        return {'cik': str(cik), 'accession': acc, 'date': filing['date']}
                    # Also check class_id if provided
                    if class_id and class_id in content:
                        return {'cik': str(cik), 'accession': acc, 'date': filing['date']}
                    break  # Index page found but wrong series; move on
            except Exception:
                continue

    return None


def get_latest_nport_for_cik(cik):
    """Most recent NPORT-P for a direct filer (CEF etc)."""
    nports = get_submissions_filings(cik)
    if nports:
        f = nports[0]
        return {'cik': str(cik), 'accession': f['accession'], 'date': f['date']}
    return None


def fetch_nport_xml(cik, accession):
    """Download primary_doc.xml from an NPORT-P filing."""
    nodash = to_nodash(accession)
    dashed = to_dashed(accession)
    # The filer CIK is embedded in the accession number's first 10 digits
    filer_cik = int(nodash[:10])
    cik_int = filer_cik if filer_cik > 0 else int(cik)
    base = f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}'

    xml_url = None

    # Try index page (.htm then .html)
    for ext in ['.htm', '.html']:
        index_url = f'{base}/{dashed}-index{ext}'
        try:
            r = requests.get(index_url, headers=EDGAR_HEADERS, timeout=12)
            if r.ok:
                matches = re.findall(
                    r'href="(/Archives/edgar/data/[^"]*primary_doc\.xml)"',
                    r.text, re.IGNORECASE
                )
                if matches:
                    xml_url = 'https://www.sec.gov' + matches[0]
                    break
                matches = re.findall(
                    r'href="(/Archives/edgar/data/[^"]*\.xml)"',
                    r.text, re.IGNORECASE
                )
                if matches:
                    xml_url = 'https://www.sec.gov' + matches[0]
                    break
        except Exception:
            continue

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
# OVERLAP
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
    ticker_upper = ticker.upper()
    now = time.time()

    if ticker_upper in _holdings_cache:
        c = _holdings_cache[ticker_upper]
        if now - c['ts'] < CACHE_TTL:
            return c['holdings'], c['date']

    # ── Path 1: Known trust ETFs ──
    if ticker_upper in HARDCODED_FILINGS:
        info = HARDCODED_FILINGS[ticker_upper]
        cik = info['cik']
        series_id = info['series_id']
        class_id = info.get('class_id')

        time.sleep(0.2)
        filing = find_nport_for_series(cik, series_id, class_id)
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

    # ── Path 2: Direct filers (CEFs etc) ──
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
    return jsonify({'status': 'Fund Correlation API is running', 'version': '9.1'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
