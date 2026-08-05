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

_ticker_cik_cache = {}   # ticker -> cik
_company_tickers = None  # full SEC tickers file, loaded once
_holdings_cache = {}     # "TICKER" -> {holdings, timestamp}
CACHE_TTL = 60 * 60 * 6  # 6 hours


def get_company_tickers():
    """Load SEC company tickers file once and cache in memory."""
    global _company_tickers
    if _company_tickers is not None:
        return _company_tickers
    url = 'https://www.sec.gov/files/company_tickers.json'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
    r.raise_for_status()
    _company_tickers = r.json()
    return _company_tickers


def get_cik_via_company_search(ticker):
    """Find CIK for a ticker using cached SEC company tickers file."""
    ticker_upper = ticker.upper()

    if ticker_upper in _ticker_cik_cache:
        return _ticker_cik_cache[ticker_upper]

    companies = get_company_tickers()
    for key, val in companies.items():
        if val.get('ticker', '').upper() == ticker_upper:
            cik = str(val['cik_str']).zfill(10)
            _ticker_cik_cache[ticker_upper] = cik
            return cik

    _ticker_cik_cache[ticker_upper] = None
    return None


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
# HOLDINGS HELPERS
# ─────────────────────────────────────────

def get_latest_nport_filing(cik):
    """Get the most recent NPORT-P accession number for a CIK."""
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


def get_holdings_from_nport(cik, accession):
    """Download and parse holdings from an NPORT-P XML filing."""
    cik_int = int(cik)

    # Try to find the XML file via the filing index page
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
    return parse_nport_xml(r.content)


def parse_nport_xml(xml_content):
    """Extract holdings list from N-PORT XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    def find_all(root, tag):
        for ns in [
            'http://www.sec.gov/edgar/nport',
            'http://www.sec.gov/edgar/nportfund',
            ''
        ]:
            results = root.findall(f'.//{{{ns}}}{tag}') if ns else root.findall(f'.//{tag}')
            if results:
                return results
        return []

    def find_text(el, tag):
        for ns in [
            'http://www.sec.gov/edgar/nport',
            'http://www.sec.gov/edgar/nportfund',
            ''
        ]:
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


def normalize_key(holding):
    """Return the best identifier for matching holdings across funds."""
    cusip = holding.get('cusip', '').strip()
    if cusip and len(cusip) >= 6 and cusip.upper() not in ('N/A', 'NA', '000000000'):
        return ('cusip', cusip)
    ticker = holding.get('ticker', '').strip()
    if ticker and ticker.upper() not in ('N/A', 'NA', ''):
        return ('ticker', ticker.upper())
    name = holding.get('name', '').upper().strip()
    for suffix in [' COM', ' COMMON STOCK', ' INC', ' CORP', ' LTD', ' CLASS A', ' CLASS B', ' CL A', ' CL B', ' CLASS C']:
        name = name.replace(suffix, '')
    return ('name', name.strip())


def calculate_overlap(holdings_a, holdings_b):
    """Calculate weighted overlap between two holdings lists."""
    map_a = {normalize_key(h): h for h in holdings_a}
    map_b = {normalize_key(h): h for h in holdings_b}

    common_keys = set(map_a.keys()) & set(map_b.keys())

    shared = []
    for key in common_keys:
        h_a = map_a[key]
        h_b = map_b[key]
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
        # Step 1: Resolve CIKs (uses cached tickers file)
        cik_a = get_cik_via_company_search(ticker_a)
        if not cik_a:
            return jsonify({'error': f'Could not find {ticker_a} in SEC EDGAR. It may not file N-PORT reports.'}), 404

        time.sleep(0.15)

        cik_b = get_cik_via_company_search(ticker_b)
        if not cik_b:
            return jsonify({'error': f'Could not find {ticker_b} in SEC EDGAR. It may not file N-PORT reports.'}), 404

        # Check holdings cache
        cache_key_a = ticker_a
        cache_key_b = ticker_b
        now = time.time()

        if cache_key_a in _holdings_cache and now - _holdings_cache[cache_key_a]['ts'] < CACHE_TTL:
            holdings_a = _holdings_cache[cache_key_a]['data']
            filing_date_a = _holdings_cache[cache_key_a]['date']
        else:
            filing_a = get_latest_nport_filing(cik_a)
            if not filing_a:
                return jsonify({'error': f'No N-PORT filing found for {ticker_a}'}), 404
            time.sleep(0.2)
            holdings_a = get_holdings_from_nport(cik_a, filing_a['accession'])
            filing_date_a = filing_a['date']
            if holdings_a:
                _holdings_cache[cache_key_a] = {'data': holdings_a, 'ts': now, 'date': filing_date_a}

        time.sleep(0.2)

        if cache_key_b in _holdings_cache and now - _holdings_cache[cache_key_b]['ts'] < CACHE_TTL:
            holdings_b = _holdings_cache[cache_key_b]['data']
            filing_date_b = _holdings_cache[cache_key_b]['date']
        else:
            filing_b = get_latest_nport_filing(cik_b)
            if not filing_b:
                return jsonify({'error': f'No N-PORT filing found for {ticker_b}'}), 404
            time.sleep(0.2)
            holdings_b = get_holdings_from_nport(cik_b, filing_b['accession'])
            filing_date_b = filing_b['date']
            if holdings_b:
                _holdings_cache[cache_key_b] = {'data': holdings_b, 'ts': now, 'date': filing_date_b}

        if not holdings_a:
            return jsonify({'error': f'Could not parse holdings for {ticker_a}'}), 500
        if not holdings_b:
            return jsonify({'error': f'Could not parse holdings for {ticker_b}'}), 500

        overlap = calculate_overlap(holdings_a, holdings_b)

        return jsonify({
            'ticker_a': ticker_a,
            'ticker_b': ticker_b,
            'filing_date_a': filing_date_a,
            'filing_date_b': filing_date_b,
            'total_holdings_a': len(holdings_a),
            'total_holdings_b': len(holdings_b),
            **overlap
        })

    except requests.exceptions.Timeout:
        return jsonify({'error': 'SEC EDGAR timed out. Please try again in a moment.'}), 504
    except requests.exceptions.HTTPError as e:
        if '429' in str(e):
            return jsonify({'error': 'SEC EDGAR is temporarily rate-limiting requests. Please wait 30 seconds and try again.'}), 429
        return jsonify({'error': f'HTTP error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Error: {str(e)}'}), 500


@app.route('/')
def index():
    return jsonify({'status': 'Fund Correlation API is running', 'version': '2.1'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
