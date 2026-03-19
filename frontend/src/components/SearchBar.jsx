import React, { useState, useEffect } from 'react';
import { Search, X, MapPin, Truck } from 'lucide-react';

const SearchBar = ({ convoys = [], onSelect }) => {
  const [query, setQuery] = useState('');
  const [focused, setFocused] = useState(false);

  const allItems = [
    ...convoys.map(c => ({ ...c, kind: "convoy" })),
    { id: "L1", label: "Raj Bhavan, Ahmedabad", kind: "location" },
    { id: "L2", label: "Science City, Ahmedabad", kind: "location" },
    { id: "L3", label: "Ahmedabad Airport", kind: "location" },
    { id: "L4", label: "Kankaria Lake, Ahmedabad", kind: "location" },
    { id: "L5", label: "Gujarat University, Ahmedabad", kind: "location" },
    { id: "L6", label: "Sabarmati Ashram, Ahmedabad", kind: "location" },
  ];

  const results = query.trim().length > 0
    ? allItems.filter(i => i.label.toLowerCase().includes(query.toLowerCase()))
    : [];

  const handleSelect = (item) => {
    setQuery(item.label);
    if (onSelect) onSelect(item);
    setFocused(false);
  };

  return (
    <div className="relative" style={{ width: '280px' }}>
      <div className="flex items-center gap-2" style={{
        backgroundColor: focused ? 'rgba(255,255,255,0.95)' : '#f8fafc',
        border: `1.5px solid ${focused ? '#fb923c' : '#e2e8f0'}`,
        borderRadius: '10px',
        padding: '0 12px',
        height: '38px',
        transition: 'all 0.3s cubic-bezier(0.4,0,0.2,1)',
        boxShadow: focused ? '0 0 0 3px rgba(234,88,12,0.12), 0 4px 12px rgba(0,0,0,0.08)' : '0 1px 3px rgba(0,0,0,0.04)',
        backdropFilter: 'blur(8px)'
      }}>
        <Search size={16} color={focused ? '#ea580c' : '#94a3b8'} />
        <input
          type="text"
          placeholder="Search convoys, locations..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setTimeout(() => setFocused(false), 150)}
          style={{
            background: 'none',
            border: 'none',
            outline: 'none',
            width: '100%',
            fontSize: '13px',
            color: 'var(--text-primary)'
          }}
        />
        {query && (
          <button onClick={() => setQuery('')} style={{ background: 'none', border: 'none', cursor: 'pointer' }}>
            <X size={14} color="#94a3b8" />
          </button>
        )}
      </div>

      {focused && results.length > 0 && (
        <div className="animate-fadeIn" style={{
          position: 'absolute',
          top: '46px',
          left: 0,
          right: 0,
          backgroundColor: 'rgba(255,255,255,0.92)',
          backdropFilter: 'blur(16px)',
          WebkitBackdropFilter: 'blur(16px)',
          borderRadius: '14px',
          boxShadow: '0 12px 32px -4px rgba(0,0,0,0.12), 0 0 0 1px rgba(234,88,12,0.08)',
          border: '1px solid rgba(234,88,12,0.15)',
          zIndex: 100,
          maxHeight: '300px',
          overflowY: 'auto'
        }}>
          {results.map((item) => (
            <div
              key={item.id}
              onMouseDown={() => handleSelect(item)}
              className="flex items-center gap-3"
              style={{
                padding: '10px 14px',
                cursor: 'pointer',
                borderBottom: '1px solid #f1f5f9',
                transition: 'background-color 0.2s ease',
              }}
              onMouseEnter={e => e.currentTarget.style.backgroundColor = 'rgba(234,88,12,0.06)'}
              onMouseLeave={e => e.currentTarget.style.backgroundColor = 'transparent'}
            >
              <div style={{
                width: '32px',
                height: '32px',
                borderRadius: '6px',
                backgroundColor: item.kind === 'convoy' ? '#fff7ed' : '#f0fdf4',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center'
              }}>
                {item.kind === 'convoy' ? <Truck size={16} color="#ea580c" /> : <MapPin size={16} color="#16a34a" />}
              </div>
              <div>
                <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>{item.label}</div>
                <div style={{ fontSize: '10px', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.025em' }}>
                  {item.kind === 'convoy' ? `Status: ${item.status}` : 'Location'}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default SearchBar;
