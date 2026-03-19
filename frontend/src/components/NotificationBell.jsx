import React from 'react';
import { Bell, Shield, Truck, Siren, TrafficCone } from 'lucide-react';

const NotificationBell = ({ alerts = [], open, onToggle }) => {
  const unreadCount = alerts.filter(a => !a.read).length;

  const typeStyles = {
    ambulance: { icon: Siren, color: "#dc2626", bg: "#fef2f2", tag: "badge-red" },
    firetruck: { icon: Siren, color: "#ea580c", bg: "#fff7ed", tag: "badge-orange" },
    police: { icon: Shield, color: "#2563eb", bg: "#eff6ff", tag: "badge-blue" },
    traffic: { icon: TrafficCone, color: "#16a34a", bg: "#f0fdf4", tag: "badge-green" }
  };

  return (
    <div className="relative">
      <button
        onClick={onToggle}
        style={{
          width: '38px',
          height: '38px',
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          backgroundColor: open ? '#fff7ed' : '#f8fafc',
          border: `1px solid ${open ? '#fed7aa' : '#e2e8f0'}`,
          cursor: 'pointer',
          transition: 'all 0.2s',
          position: 'relative'
        }}
      >
        <Bell size={20} color={open ? '#ea580c' : '#64748b'} />
        {unreadCount > 0 && (
          <div style={{
            position: 'absolute',
            top: '-2px',
            right: '-2px',
            width: '18px',
            height: '18px',
            backgroundColor: '#dc2626',
            borderRadius: '50%',
            color: 'white',
            fontSize: '10px',
            fontWeight: 700,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            border: '2px solid #ffffff'
          }}>
            {unreadCount}
          </div>
        )}
      </button>

      {open && (
        <div style={{
          position: 'absolute',
          top: '46px',
          right: 0,
          width: '340px',
          backgroundColor: '#ffffff',
          borderRadius: '12px',
          boxShadow: '0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)',
          border: '1px solid #e2e8f0',
          zIndex: 100,
          overflow: 'hidden'
        }}>
          <div style={{ padding: '16px', borderBottom: '1px solid #f1f5f9', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div>
              <h3 style={{ fontSize: '14px', fontWeight: 700 }}>Clearance Requests</h3>
              <p style={{ fontSize: '11px', color: '#64748b' }}>Operations Center Alerts</p>
            </div>
            {unreadCount > 0 && <span className="badge badge-red">{unreadCount} New</span>}
          </div>

          <div className="sp" style={{ maxHeight: '360px', overflowY: 'auto' }}>
            {alerts.length > 0 ? (
              alerts.map((alert) => {
                const styles = typeStyles[alert.type] || typeStyles.police;
                const Icon = styles.icon;
                return (
                  <div
                    key={alert.id}
                    style={{
                      padding: '12px 16px',
                      borderBottom: '1px solid #f1f5f9',
                      backgroundColor: alert.read ? '#ffffff' : '#fffbf5',
                      position: 'relative'
                    }}
                  >
                    <div className="flex gap-3">
                      <div style={{
                        width: '36px',
                        height: '36px',
                        borderRadius: '8px',
                        backgroundColor: styles.bg,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        flexShrink: 0
                      }}>
                        <Icon size={18} color={styles.color} />
                      </div>
                      <div style={{ flex: 1 }}>
                        <div className="flex justify-between items-start">
                          <span className={`badge ${styles.tag}`} style={{ marginBottom: '12px', border: 'none', minWidth: 'auto', padding: '1px 8px' }}>{alert.tag}</span>
                          <span style={{ fontSize: '10px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>{alert.time}</span>
                        </div>
                        <p style={{ 
                          fontSize: '12px', 
                          color: 'var(--text-primary)', 
                          fontWeight: alert.read ? 400 : 600,
                          lineHeight: '1.4'
                        }}>
                          {alert.message}
                        </p>
                        {!alert.read && (
                          <div className="flex gap-2" style={{ marginTop: '10px' }}>
                            <button className="badge badge-green" style={{ border: 'none', cursor: 'pointer', padding: '4px 10px' }}>Approve</button>
                            <button className="badge badge-red" style={{ border: 'none', cursor: 'pointer', padding: '4px 10px' }}>Defer</button>
                          </div>
                        )}
                      </div>
                      {!alert.read && (
                        <div style={{ 
                          width: '6px', 
                          height: '6px', 
                          backgroundColor: '#ea580c', 
                          borderRadius: '50%', 
                          marginTop: '4px' 
                        }} />
                      )}
                    </div>
                  </div>
                );
              })
            ) : (
              <div style={{ padding: '40px 20px', textCenter: 'center', color: '#94a3b8', fontSize: '13px' }}>
                No active clearance requests
              </div>
            )}
          </div>

          <div style={{ padding: '12px', textAlign: 'center', backgroundColor: '#f8fafc' }}>
            <button style={{ color: '#ea580c', fontSize: '11px', fontWeight: 700, background: 'none', border: 'none', cursor: 'pointer' }}>
              View all alerts →
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default NotificationBell;
