import React, { useState } from 'react';
import { ConvoyProvider } from './context/ConvoyContext';
import VVIPDashboard from './components/VVIPDashboard';
import RoutePlanning from './views/RoutePlanning';
import InterDeptComms from './views/InterDeptComms';
import CitizenAdvisory from './views/CitizenAdvisory';
import GreenCorridor from './views/GreenCorridor';

function App() {
  const [currentView, setCurrentView] = useState('dashboard');

  const renderView = () => {
    switch (currentView) {
      case 'route-planning':
        return <RoutePlanning navigate={(view) => setCurrentView(view)} />;
      case 'comms':
        return <InterDeptComms navigate={(view) => setCurrentView(view)} />;
      case 'citizen-advisory':
        return <CitizenAdvisory navigate={(view) => setCurrentView(view)} />;
      case 'green-corridor':
        return <GreenCorridor navigate={(view) => setCurrentView(view)} />;
      default:
        return <VVIPDashboard navigate={(view) => setCurrentView(view)} />;
    }
  };

  return (
    <ConvoyProvider>
      <div className="App noise-texture">
        <div className="vignette-overlay" />
        {renderView()}
      </div>
    </ConvoyProvider>
  );
}

export default App;

