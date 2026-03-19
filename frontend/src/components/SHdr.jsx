import React from 'react';

const SHdr = ({ title, badge, badgeColor = "orange" }) => {
  const colorMap = {
    orange: "badge-orange",
    green: "badge-green",
    red: "badge-red",
    blue: "badge-blue"
  };

  return (
    <div className="flex items-center justify-between top-accent-sweep" style={{ 
      position: 'sticky', 
      top: 0, 
      zIndex: 2, 
      backgroundColor: 'rgba(15,23,42,0.92)', 
      backdropFilter: 'blur(12px)',
      WebkitBackdropFilter: 'blur(12px)',
      padding: '12px 14px',
      borderBottom: '1px solid #334155',
      boxShadow: '0 1px 4px rgba(0,0,0,0.2)',
    }}>
      <div className="flex items-center gap-2">
        <div style={{ 
          width: '3px', 
          height: '16px', 
          background: 'linear-gradient(180deg, #ea580c, #f97316)', 
          borderRadius: '2px',
          boxShadow: '0 0 6px rgba(234,88,12,0.3)' 
        }} />
        <h3 style={{ 
          fontSize: '11px', 
          fontWeight: 700, 
          letterSpacing: '0.05em', 
          color: '#e2e8f0',
          textTransform: 'uppercase'
        }}>{title}</h3>
      </div>
      {badge && <span className={`badge ${colorMap[badgeColor]}`}>{badge}</span>}
    </div>
  );
};

export default SHdr;
