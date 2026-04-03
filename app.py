from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf

app = Flask(__name__)
CORS(app)

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

@app.route('/')
def index():
    return jsonify({'status': 'Fund Correlation API is running'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
