from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import requests
import xml.etree.ElementTree as ET
import time

app = Flask(__name__)
CORS(app)

EDGAR_HEADERS = {
    'User-Agent': 'FundCorrelationTool contact@fundcorrelation.app',
    'Accept-Encoding': 'gzip, deflate'
}

# ─────────────────────────────────────────
# PRICES ENDPOINT (existing)
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
# HOLDINGS OVERLAP ENDPOINT (new)
# ─────────────────────────────────────────

def get_cik_for_ticker(ticker):
    """Look up a fund's CIK number from its ticker using SEC EDGAR."""
    url = f'https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2020-01-01&forms=NPORT-P'
    # Use the company search endpoint instead
    url = f'https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK={ticker}&type=NPORT-P&dateb=&owner=include&count=5&search_text=&action=getcompany&output=atom'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
    r.raise_for_status()

    # Parse the Atom feed to get CIK
    root = ET.fromstring(r.content)
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    entries = root.findall('atom:entry', ns)
    if not entries:
        return None

    # Extract CIK from the first entry's company-info link
    for entry in entries:
        company_info = entry.find('atom:content/atom:company-info', ns)
        if company_info is not None:
            cik_el = company_info.find('atom:cik', ns)
            if cik_el is not None:
                return cik_el.text.zfill(10)

    # Fallback: try the submissions API
    url2 = f'https://data.sec.gov/submissions/CIK{ticker}.json'
    try:
        r2 = requests.get(url2, headers=EDGAR_HEADERS, timeout=10)
        if r2.ok:
            data = r2.json()
            return str(data.get('cik', '')).zfill(10)
    except:
        pass

    return None


def get_cik_via_company_search(ticker):
    """Search EDGAR company search for a ticker to find its CIK."""
    url = f'https://efts.sec.gov/LATEST/search-index?q=ticker%3A{ticker}&forms=NPORT-P&dateRange=custom&startdt=2023-01-01'
    try:
        r = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
        if r.ok:
            data = r.json()
            hits = data.get('hits', {}).get('hits', [])
            if hits:
                return str(hits[0].get('_source', {}).get('period_of_report', ''))
    except:
        pass

    # Use the ticker-to-CIK mapping from SEC
    url2 = 'https://www.sec.gov/files/company_tickers.json'
    r2 = requests.get(url2, headers=EDGAR_HEADERS, timeout=15)
    r2.raise_for_status()
    companies = r2.json()
    ticker_upper = ticker.upper()
    for key, val in companies.items():
        if val.get('ticker', '').upper() == ticker_upper:
            return str(val['cik_str']).zfill(10)
    return None


def get_latest_nport_filing(cik):
    """Fetch the most recent NPORT-P filing accession number for a given CIK."""
    url = f'https://data.sec.gov/submissions/CIK{cik}.json'
    r = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()

    filings = data.get('filings', {}).get('recent', {})
    form_types = filings.get('form', [])
    accession_numbers = filings.get('accessionNumber', [])
    filing_dates = filings.get('filingDate', [])

    # Find the most recent NPORT-P
    for i, form in enumerate(form_types):
        if form in ('NPORT-P', 'NPORT-P/A'):
            return {
                'accession': accession_numbers[i].replace('-', ''),
                'accession_formatted': accession_numbers[i],
                'date': filing_dates[i]
            }
    return None


def get_holdings_from_nport(cik, accession):
    """Parse holdings from an NPORT-P filing XML."""
    # Construct the filing index URL
    accession_dashes = f'{accession[:10]}-{accession[10:12]}-{accession[12:]}'
    index_url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/primary_doc.xml'

    # Try to get the filing index first to find the right XML file
    index_page_url = f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=NPORT-P&dateb=&owner=include&count=1'

    # Fetch the filing index
    filing_index_url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{accession}-index.htm'
    r = requests.get(filing_index_url, headers=EDGAR_HEADERS, timeout=15)

    xml_url = None
    if r.ok:
        # Find the primary XML document
        content = r.text
        for line in content.split('\n'):
            if 'primary_doc.xml' in line or ('NPORT' in line.upper() and '.xml' in line.lower()):
                import re
                match = re.search(r'href="([^"]+\.xml)"', line)
                if match:
                    xml_url = 'https://www.sec.gov' + match.group(1)
                    break

    if not xml_url:
        xml_url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/primary_doc.xml'

    time.sleep(0.1)  # Be polite to SEC servers
    r = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=30)
    r.raise_for_status()

    return parse_nport_xml(r.content)


def parse_nport_xml(xml_content):
    """Extract holdings from N-PORT XML content."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    # N-PORT XML uses namespaces
    ns_map = {
        'nport': 'http://www.sec.gov/edgar/nport',
        '': ''
    }

    holdings = []

    # Try with namespace first, then without
    def find_all(root, tag):
        # Try common N-PORT namespaces
        namespaces = [
            'http://www.sec.gov/edgar/nport',
            'http://www.sec.gov/edgar/nportfund',
            ''
        ]
        for ns in namespaces:
            if ns:
                results = root.findall(f'.//{{{ns}}}{tag}')
            else:
                results = root.findall(f'.//{tag}')
            if results:
                return results
        return []

    def find_text(el, tag):
        namespaces = [
            'http://www.sec.gov/edgar/nport',
            'http://www.sec.gov/edgar/nportfund',
            ''
        ]
        for ns in namespaces:
            if ns:
                child = el.find(f'{{{ns}}}{tag}')
            else:
                child = el.find(tag)
            if child is not None and child.text:
                return child.text.strip()
        return ''

    invstOrSecs = find_all(root, 'invstOrSec')

    # Get total net assets for weight calculation
    total_assets = 0
    for tag in ['netAssets', 'totAssets']:
        els = find_all(root, tag)
        if els and els[0].text:
            try:
                total_assets = float(els[0].text)
                break
            except:
                pass

    for invst in invstOrSecs:
        name = find_text(invst, 'name')
        cusip = find_text(invst, 'cusip')
        ticker = find_text(invst, 'ticker')
        val_usd = find_text(invst, 'valUSD')
        pct_val = find_text(invst, 'pctVal')

        if not name:
            continue

        # Calculate weight
        weight = 0.0
        if pct_val:
            try:
                weight = float(pct_val)
            except:
                pass
        elif val_usd and total_assets > 0:
            try:
                weight = (float(val_usd) / total_assets) * 100
            except:
                pass

        holdings.append({
            'name': name,
            'cusip': cusip,
            'ticker': ticker,
            'weight': round(weight, 4)
        })

    # Sort by weight descending
    holdings.sort(key=lambda x: x['weight'], reverse=True)
    return holdings


def normalize_key(holding):
    """Return the best identifier for matching holdings across funds."""
    if holding.get('cusip') and len(holding['cusip']) >= 6:
        return ('cusip', holding['cusip'])
    if holding.get('ticker') and holding['ticker'].strip():
        return ('ticker', holding['ticker'].upper().strip())
    # Fall back to normalized name
    name = holding.get('name', '').upper().strip()
    # Remove common suffixes for better matching
    for suffix in [' COM', ' COMMON', ' INC', ' CORP', ' LTD', ' CLASS A', ' CLASS B', ' CL A', ' CL B']:
        name = name.replace(suffix, '')
    return ('name', name.strip())


def calculate_overlap(holdings_a, holdings_b):
    """Calculate weighted overlap between two sets of holdings."""
    # Build lookup dictionaries
    map_a = {}
    for h in holdings_a:
        key = normalize_key(h)
        map_a[key] = h

    map_b = {}
    for h in holdings_b:
        key = normalize_key(h)
        map_b[key] = h

    # Find shared holdings
    shared = []
    keys_a = set(map_a.keys())
    keys_b = set(map_b.keys())
    common_keys = keys_a & keys_b

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

    # Sort shared by average weight
    shared.sort(key=lambda x: x['avg_weight'], reverse=True)

    # Calculate overlap percentages
    overlap_by_a = sum(h['weight_a'] for h in shared)
    overlap_by_b = sum(h['weight_b'] for h in shared)
    overlap_avg = (overlap_by_a + overlap_by_b) / 2

    return {
        'shared_count': len(shared),
        'overlap_pct_a': round(overlap_by_a, 2),
        'overlap_pct_b': round(overlap_by_b, 2),
        'overlap_pct_avg': round(overlap_avg, 2),
        'shared_holdings': shared[:50]  # Return top 50 shared
    }


@app.route('/api/holdings-overlap')
def holdings_overlap():
    ticker_a = request.args.get('tickerA', '').upper().strip()
    ticker_b = request.args.get('tickerB', '').upper().strip()

    if not ticker_a or not ticker_b:
        return jsonify({'error': 'Both tickerA and tickerB are required'}), 400

    try:
        # Step 1: Look up CIKs
        cik_a = get_cik_via_company_search(ticker_a)
        if not cik_a:
            return jsonify({'error': f'Could not find SEC EDGAR filing for {ticker_a}. It may not file N-PORT reports.'}), 404

        time.sleep(0.2)  # Respect SEC rate limits

        cik_b = get_cik_via_company_search(ticker_b)
        if not cik_b:
            return jsonify({'error': f'Could not find SEC EDGAR filing for {ticker_b}. It may not file N-PORT reports.'}), 404

        # Step 2: Get latest N-PORT filing for each
        filing_a = get_latest_nport_filing(cik_a)
        if not filing_a:
            return jsonify({'error': f'No N-PORT filing found for {ticker_a}'}), 404

        time.sleep(0.2)

        filing_b = get_latest_nport_filing(cik_b)
        if not filing_b:
            return jsonify({'error': f'No N-PORT filing found for {ticker_b}'}), 404

        # Step 3: Fetch and parse holdings
        holdings_a = get_holdings_from_nport(cik_a, filing_a['accession'])
        time.sleep(0.3)
        holdings_b = get_holdings_from_nport(cik_b, filing_b['accession'])

        if not holdings_a:
            return jsonify({'error': f'Could not parse holdings for {ticker_a}'}), 500
        if not holdings_b:
            return jsonify({'error': f'Could not parse holdings for {ticker_b}'}), 500

        # Step 4: Calculate overlap
        overlap = calculate_overlap(holdings_a, holdings_b)

        return jsonify({
            'ticker_a': ticker_a,
            'ticker_b': ticker_b,
            'filing_date_a': filing_a['date'],
            'filing_date_b': filing_b['date'],
            'total_holdings_a': len(holdings_a),
            'total_holdings_b': len(holdings_b),
            **overlap
        })

    except requests.exceptions.Timeout:
        return jsonify({'error': 'SEC EDGAR request timed out. Please try again.'}), 504
    except Exception as e:
        return jsonify({'error': f'Error fetching holdings: {str(e)}'}), 500


@app.route('/')
def index():
    return jsonify({'status': 'Fund Correlation API is running', 'version': '2.0'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)

