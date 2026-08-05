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
# IN-MEMORY CACHE
# ─────────────────────────────────────────

_company_tickers = None       # full SEC tickers file, loaded once
_ticker_to_meta = {}          # ticker -> {cik, series_id}
_holdings_cache = {}          # ticker -> {holdings, date, ts}
CACHE_TTL = 60 * 60 * 6      # 6 hours


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

        prices = []
        for date, row in hist.iterrows():
            prices.append({
                'date': date.strftime('%Y-%m-%d'),
                'price': round(float(row['Close']), 4)
            })

        return jsonify({'ticker': ticker, 'prices': prices})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# EDGAR HELPERS
# ─────────────────────────────────────────

def get_company_tickers():
    """Load SEC company tickers file once and cache it."""
    global _company_tickers
    if _company_tickers is not None:
        return _company_tickers
    url = 'https://www.sec.gov/files/company_tickers.json'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    _company_tickers = r.json()
    return _company_tickers


def resolve_ticker_to_cik(ticker):
    """
    Find a fund's CIK from its ticker.
    Handles both direct filers (CEFs, some ETFs) and ETF trusts.
    Returns {cik, series_id} where series_id may be None for direct filers.
    """
    ticker_upper = ticker.upper()

    if ticker_upper in _ticker_to_meta:
        return _ticker_to_meta[ticker_upper]

    # Step 1: Try the standard company tickers file (works for CEFs and many ETFs)
    companies = get_company_tickers()
    for key, val in companies.items():
        if val.get('ticker', '').upper() == ticker_upper:
            meta = {'cik': str(val['cik_str']).zfill(10), 'series_id': None}
            _ticker_to_meta[ticker_upper] = meta
            return meta

    # Step 2: Search EDGAR full-text search for the ticker as a series within a trust
    search_url = f'https://efts.sec.gov/LATEST/search-index?q=%22{ticker_upper}%22&forms=NPORT-P&dateRange=custom&startdt=2023-01-01'
    try:
        r = requests.get(search_url, headers=EDGAR_HEADERS, timeout=15)
        if r.ok:
            data = r.json()
            hits = data.get('hits', {}).get('hits', [])
            for hit in hits:
                src = hit.get('_source', {})
                entity_id = src.get('entity_id') or src.get('file_num', '')
                if entity_id:
                    break
    except Exception:
        pass

    # Step 3: Search the EDGAR company search by ticker
    search_url2 = f'https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK={ticker_upper}&type=NPORT-P&dateb=&owner=include&count=5&search_text=&action=getcompany&output=atom'
    try:
        r = requests.get(search_url2, headers=EDGAR_HEADERS, timeout=15)
        if r.ok:
            root = ET.fromstring(r.content)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            for entry in root.findall('atom:entry', ns):
                content = entry.find('atom:content', ns)
                if content is not None:
                    cik_el = content.find('atom:company-info/atom:cik', ns)
                    if cik_el is not None:
                        meta = {'cik': cik_el.text.zfill(10), 'series_id': None}
                        _ticker_to_meta[ticker_upper] = meta
                        return meta
    except Exception:
        pass

    _ticker_to_meta[ticker_upper] = None
    return None


def get_series_id_for_ticker(ticker, trust_cik):
    """
    Search a trust's filings for the series ID matching a given ticker.
    Uses the EDGAR submissions API to find series info.
    """
    url = f'https://data.sec.gov/submissions/CIK{trust_cik}.json'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    # Check series info if available
    series_data = data.get('series', [])

    # Search EDGAR for this ticker's series ID via full-text search
    # The series ID format is S000XXXXXX
    search_url = (
        f'https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22+%22{trust_cik}%22'
        f'&forms=NPORT-P&dateRange=custom&startdt=2024-01-01'
    )
    try:
        r2 = requests.get(search_url, headers=EDGAR_HEADERS, timeout=15)
        if r2.ok:
            hits = r2.json().get('hits', {}).get('hits', [])
            for hit in hits:
                series_id = hit.get('_source', {}).get('series_id')
                if series_id:
                    return series_id
    except Exception:
        pass

    return None


def get_latest_nport_filing(cik, series_id=None):
    """Get the most recent NPORT-P accession for a CIK, optionally filtered by series."""
    url = f'https://data.sec.gov/submissions/CIK{cik}.json'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    filings = data.get('filings', {}).get('recent', {})
    form_types = filings.get('form', [])
    accession_numbers = filings.get('accessionNumber', [])
    filing_dates = filings.get('filingDate', [])

    for i, form in enumerate(form_types):
        if form in ('NPORT-P', 'NPORT-P/A'):
            return {
                'accession': accession_numbers[i].replace('-', ''),
                'accession_formatted': accession_numbers[i],
                'date': filing_dates[i]
            }
    return None


def get_holdings_from_nport(cik, accession, series_id=None, target_ticker=None):
    """
    Download and parse holdings from an NPORT-P filing.
    If series_id is provided, filters to just that fund's holdings within a trust.
    """
    cik_int = int(cik)

    # Find the primary XML file
    index_url = f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{accession}-index.htm'
    xml_url = None

    try:
        r = requests.get(index_url, headers=EDGAR_HEADERS, timeout=15)
        if r.ok:
            matches = re.findall(r'href="(/Archives/edgar/data/[^"]+primary_doc\.xml)"', r.text)
            if matches:
                xml_url = 'https://www.sec.gov' + matches[0]
            else:
                matches = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', r.text)
                if matches:
                    xml_url = 'https://www.sec.gov' + matches[0]
    except Exception:
        pass

    if not xml_url:
        xml_url = f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/primary_doc.xml'

    time.sleep(0.15)
    r = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=30)
    r.raise_for_status()

    return parse_nport_xml(r.content, series_id=series_id, target_ticker=target_ticker)


def parse_nport_xml(xml_content, series_id=None, target_ticker=None):
    """
    Parse N-PORT XML. If series_id given, extracts only that series' holdings.
    N-PORT trust filings contain multiple <formData> blocks, one per fund.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    NPORT_NS = [
        'http://www.sec.gov/edgar/nport',
        'http://www.sec.gov/edgar/nportfund',
        ''
    ]

    def find_all(node, tag):
        for ns in NPORT_NS:
            results = node.findall(f'.//{{{ns}}}{tag}') if ns else node.findall(f'.//{tag}')
            if results:
                return results
        return []

    def find_text(el, tag):
        for ns in NPORT_NS:
            child = el.find(f'{{{ns}}}{tag}') if ns else el.find(tag)
            if child is not None and child.text:
                return child.text.strip()
        return ''

    def extract_holdings_from_node(node):
        """Extract holdings list from a formData or root node."""
        invstOrSecs = find_all(node, 'invstOrSec')

        total_assets = 0
        for tag in ['netAssets', 'totAssets']:
            els = find_all(node, tag)
            if els and els[0].text:
                try:
                    total_assets = float(els[0].text)
                    break
                except Exception:
                    pass

        holdings = []
        for invst in invstOrSecs:
            name = find_text(invst, 'name')
            cusip = find_text(invst, 'cusip')
            ticker = find_text(invst, 'ticker')
            val_usd = find_text(invst, 'valUSD')
            pct_val = find_text(invst, 'pctVal')

            if not name:
                continue

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
                'name': name,
                'cusip': cusip,
                'ticker': ticker,
                'weight': round(weight, 4)
            })

        holdings.sort(key=lambda x: x['weight'], reverse=True)
        return holdings

    # If this is a trust filing with multiple series, find the right one
    if series_id or target_ticker:
        # Look for formData blocks (one per series in a trust N-PORT)
        for ns in NPORT_NS:
            if ns:
                form_data_blocks = root.findall(f'.//{{{ns}}}formData')
            else:
                form_data_blocks = root.findall('.//formData')

            if not form_data_blocks:
                continue

            for block in form_data_blocks:
                # Check series ID match
                block_series_id = find_text(block, 'seriesId')
                if series_id and block_series_id == series_id:
                    return extract_holdings_from_node(block)

                # Check ticker match in genInfo
                block_series_name = find_text(block, 'seriesName')
                if target_ticker and target_ticker.upper() in block_series_name.upper():
                    return extract_holdings_from_node(block)

    # Default: parse the whole document (works for single-fund filers like CEFs)
    return extract_holdings_from_node(root)


def normalize_key(holding):
    """Best identifier for matching holdings across funds."""
    cusip = holding.get('cusip', '').strip()
    if cusip and len(cusip) >= 6 and cusip.upper() not in ('N/A', 'NA', '000000000', ''):
        return ('cusip', cusip)
    ticker = holding.get('ticker', '').strip().upper()
    if ticker and ticker not in ('N/A', 'NA', ''):
        return ('ticker', ticker)
    name = holding.get('name', '').upper().strip()
    for suffix in [' COMMON STOCK', ' COM', ' INC', ' CORP', ' LTD',
                   ' CLASS A', ' CLASS B', ' CL A', ' CL B', ' CLASS C']:
        name = name.replace(suffix, '')
    return ('name', name.strip())


def calculate_overlap(holdings_a, holdings_b):
    """Calculate weighted overlap between two holdings lists."""
    map_a = {normalize_key(h): h for h in holdings_a}
    map_b = {normalize_key(h): h for h in holdings_b}
    common_keys = set(map_a.keys()) & set(map_b.keys())

    shared = []
    for key in common_keys:
        h_a, h_b = map_a[key], map_b[key]
        shared.append({
            'name': h_a['name'],
            'ticker': h_a.get('ticker') or h_b.get('ticker') or '',
            'cusip': h_a.get('cusip') or h_b.get('cusip') or '',
            'weight_a': h_a['weight'],
            'weight_b': h_b['weight'],
            'avg_weight': round((h_a['weight'] + h_b['weight']) / 2, 4)
        })

    shared.sort(key=lambda x: x['avg_weight'], reverse=True)

    overlap_by_a = sum(h['weight_a'] for h in shared)
    overlap_by_b = sum(h['weight_b'] for h in shared)

    return {
        'shared_count': len(shared),
        'overlap_pct_a': round(overlap_by_a, 2),
        'overlap_pct_b': round(overlap_by_b, 2),
        'overlap_pct_avg': round((overlap_by_a + overlap_by_b) / 2, 2),
        'shared_holdings': shared[:50]
    }


# Known trust overrides: ticker -> {cik, series_id}
# These are ETFs that file under a parent trust CIK rather than their own
KNOWN_TRUST_OVERRIDES = {
    'GPIX': {'cik': '0001479026', 'series_id': 'S000081511'},
    'GPIQ': {'cik': '0001479026', 'series_id': 'S000081510'},
    # iShares examples (CIK 1100663 = iShares Trust)
    'IVV':  {'cik': '0001100663', 'series_id': None},
    'AGG':  {'cik': '0001100663', 'series_id': None},
    'SPY':  {'cik': '0000884394', 'series_id': None},
}


def get_fund_holdings(ticker):
    """
    Master function: fetch and return holdings for any ticker.
    Handles CEFs (direct filers) and ETFs (trust filers).
    Uses cache to avoid repeat SEC requests.
    """
    ticker_upper = ticker.upper()
    now = time.time()

    # Return from cache if fresh
    if ticker_upper in _holdings_cache:
        cached = _holdings_cache[ticker_upper]
        if now - cached['ts'] < CACHE_TTL:
            return cached['holdings'], cached['date']

    # Check known trust overrides first
    override = KNOWN_TRUST_OVERRIDES.get(ticker_upper)
    if override:
        cik = override['cik'].zfill(10)
        series_id = override.get('series_id')
    else:
        meta = resolve_ticker_to_cik(ticker_upper)
        if not meta:
            return None, None
        cik = meta['cik']
        series_id = meta.get('series_id')

    time.sleep(0.2)

    filing = get_latest_nport_filing(cik, series_id)
    if not filing:
        return None, None

    time.sleep(0.2)

    holdings = get_holdings_from_nport(
        cik,
        filing['accession'],
        series_id=series_id,
        target_ticker=ticker_upper
    )

    if holdings:
        _holdings_cache[ticker_upper] = {
            'holdings': holdings,
            'date': filing['date'],
            'ts': now
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
            return jsonify({
                'error': f'Could not find holdings for {ticker_a}. '
                         f'It may not file N-PORT reports with the SEC, '
                         f'or may need to be added to the known trusts list.'
            }), 404

        time.sleep(0.3)

        holdings_b, date_b = get_fund_holdings(ticker_b)
        if not holdings_b:
            return jsonify({
                'error': f'Could not find holdings for {ticker_b}. '
                         f'It may not file N-PORT reports with the SEC.'
            }), 404

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
    return jsonify({'status': 'Fund Correlation API is running', 'version': '3.0'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
