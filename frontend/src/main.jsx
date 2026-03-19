import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'
import 'leaflet/dist/leaflet.css'

class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null, info: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error, info) { this.setState({ info }); console.error('ErrorBoundary caught:', error, info); }
  render() {
    if (this.state.error) {
      return React.createElement('div', { style: { padding: '40px', color: '#ff6b6b', backgroundColor: '#0f172a', fontFamily: 'monospace', whiteSpace: 'pre-wrap' } },
        React.createElement('h1', null, 'Runtime Error'),
        React.createElement('pre', null, String(this.state.error)),
        React.createElement('pre', { style: { color: '#94a3b8', marginTop: '20px' } }, this.state.info?.componentStack)
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
