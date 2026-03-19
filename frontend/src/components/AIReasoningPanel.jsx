import React, { useState, useMemo, useRef, useEffect } from 'react';
import { Brain, ChevronDown, ChevronRight, Activity, AlertTriangle, CheckCircle, Clock, Shield, X, Maximize2, Minimize2, Send } from 'lucide-react';
import { useConvoy } from '../context/ConvoyContext';

const TYPE_STYLE = {
  thought: { color: '#cbd5e1', bg: 'transparent', icon: Brain, label: 'Thought' },
  tool: { color: '#3b82f6', bg: 'rgba(59,130,246,0.06)', icon: Activity, label: 'Tool' },
  decision: { color: '#ea580c', bg: 'rgba(234,88,12,0.06)', icon: Shield, label: 'Decision' },
  error: { color: '#ef4444', bg: 'rgba(239,68,68,0.06)', icon: AlertTriangle, label: 'Error' },
  system: { color: '#8b5cf6', bg: 'rgba(139,92,246,0.06)', icon: CheckCircle, label: 'System' },
  plan: { color: '#16a34a', bg: 'rgba(22,163,74,0.06)', icon: CheckCircle, label: 'Plan' },
  escort: { color: '#16a34a', bg: 'rgba(22,163,74,0.06)', icon: Shield, label: 'Escort' },
  clear: { color: '#2563eb', bg: 'rgba(37,99,235,0.06)', icon: CheckCircle, label: 'Clear' },
};

const AIReasoningPanel = ({ visible, onClose }) => {
  const { aiReasoning, lifecycle, planResult, gpuHealth, chatStreaming, sendChatMessage, flyToSegment } = useConvoy();

  // Parse segment references like "Seg 1234", "segment 1234", "segment_id: 1234" into clickable chips
  const renderTextWithSegLinks = (text) => {
    if (!text || typeof text !== 'string') return text;
    const parts = text.split(/(\bseg(?:ment)?(?:_id)?[:\s]?\s*\d+)/gi);
    return parts.map((part, idx) => {
      const m = part.match(/\bseg(?:ment)?(?:_id)?[:\s]?\s*(\d+)/i);
      if (m) {
        const segId = parseInt(m[1], 10);
        return (
          <span
            key={idx}
            className="seg-chip"
            onClick={(e) => { e.stopPropagation(); flyToSegment(segId); }}
            title={`Fly to Segment ${segId}`}
          >
            Seg {segId}
          </span>
        );
      }
      return part;
    });
  };
  const [expanded, setExpanded] = useState(true);
  const [filter, setFilter] = useState('all');
  const [chatInput, setChatInput] = useState('');
  const inputRef = useRef(null);
  const scrollRef = useRef(null);

  const filtered = useMemo(() => {
    if (filter === 'all') return aiReasoning;
    return aiReasoning.filter((r) => r.type === filter);
  }, [aiReasoning, filter]);

  const stats = useMemo(() => {
    const counts = { thought: 0, tool: 0, decision: 0, error: 0 };
    aiReasoning.forEach((r) => { counts[r.type] = (counts[r.type] || 0) + 1; });
    return counts;
  }, [aiReasoning]);

  // Auto-scroll when new entries arrive
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [aiReasoning.length]);

  const handleSendChat = async () => {
    const msg = chatInput.trim();
    if (!msg || chatStreaming) return;
    setChatInput('');
    try {
      await sendChatMessage(msg);
    } catch { /* errors shown in trace */ }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendChat();
    }
  };

  if (!visible) return null;

  return (
    <div style={{
      position: 'fixed',
      bottom: '16px',
      right: '16px',
      width: expanded ? '420px' : '320px',
      maxHeight: expanded ? '70vh' : '48px',
      backgroundColor: '#1e293b',
      borderRadius: '14px',
      border: '1px solid rgba(51,65,85,0.8)',
      boxShadow: '0 20px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(234,88,12,0.05)',
      display: 'flex',
      flexDirection: 'column',
      zIndex: 2000,
      overflow: 'hidden',
      transition: 'all 0.2s ease',
      fontFamily: 'var(--font-secondary)',
    }}>
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 14px',
          backgroundColor: '#0f172a',
          borderBottom: expanded ? '1px solid rgba(51,65,85,0.7)' : 'none',
          cursor: 'pointer',
          flexShrink: 0,
        }}
        onClick={() => setExpanded((e) => !e)}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Brain size={16} color="#ea580c" style={{ filter: 'drop-shadow(0 0 4px rgba(234,88,12,0.3))' }} />
          <span style={{ fontSize: '12px', fontWeight: 700, color: 'white', letterSpacing: '0.02em' }}>AI Reasoning Trace</span>
          <span style={{ fontSize: '9px', padding: '2px 6px', backgroundColor: '#334155', borderRadius: '4px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
            {aiReasoning.length} entries
          </span>
          {lifecycle !== 'idle' && (
            <span style={{ fontSize: '8px', padding: '1px 5px', borderRadius: '3px', backgroundColor: lifecycle === 'active' ? '#16a34a22' : '#ea580c22', color: lifecycle === 'active' ? '#16a34a' : '#ea580c', fontWeight: 700 }}>
              {lifecycle.toUpperCase()}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: '6px' }}>
          <button onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#94a3b8', padding: '2px' }}>
            {expanded ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
          </button>
          <button onClick={(e) => { e.stopPropagation(); onClose(); }} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#94a3b8', padding: '2px' }}>
            <X size={12} />
          </button>
        </div>
      </div>

      {expanded && (
        <>
          {/* Stats Strip */}
          <div style={{ display: 'flex', gap: '6px', padding: '8px 14px', borderBottom: '1px solid #334155', flexShrink: 0 }}>
            {Object.entries(TYPE_STYLE).map(([key, s]) => (
              <button
                key={key}
                onClick={() => setFilter(filter === key ? 'all' : key)}
                style={{
                  display: 'flex', alignItems: 'center', gap: '4px',
                  padding: '3px 8px', borderRadius: '4px', fontSize: '9px', fontWeight: 700,
                  border: filter === key ? `1px solid ${s.color}` : '1px solid #334155',
                  backgroundColor: filter === key ? s.bg : 'transparent',
                  color: s.color, cursor: 'pointer',
                }}
              >
                <s.icon size={8} /> {stats[key] || 0}
              </button>
            ))}
            {gpuHealth && (
              <div style={{ marginLeft: 'auto', fontSize: '8px', color: '#64748b', fontFamily: 'var(--font-mono)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                VRAM {Math.round((gpuHealth.vramUsedMb / gpuHealth.vramTotalMb) * 100)}%
              </div>
            )}
          </div>

          {/* Plan Summary Bar */}
          {planResult && (
            <div style={{ padding: '6px 14px', backgroundColor: '#0f172a', borderBottom: '1px solid #334155', display: 'flex', gap: '12px', fontSize: '9px', color: '#94a3b8', fontFamily: 'var(--font-mono)', flexShrink: 0 }}>
              <span>Route: {planResult.primary_route?.route_id?.slice(0, 8) || '—'}</span>
              <span>Score: {planResult.primary_route?.score?.toFixed(2) || '—'}</span>
              <span>Conf: {planResult.confidence || '—'}</span>
              <span style={{ color: planResult.security_compliant ? '#16a34a' : '#ef4444' }}>
                Sec: {planResult.security_compliant ? 'PASS' : 'FAIL'}
              </span>
            </div>
          )}

          {/* Entries */}
          <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '8px 14px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {filtered.length === 0 && (
              <div style={{ textAlign: 'center', padding: '30px', color: '#475569', fontSize: '11px' }}>
                No reasoning entries yet. Start a plan or type a message below.
              </div>
            )}
            {filtered.map((entry, i) => {
              const style = TYPE_STYLE[entry.type] || TYPE_STYLE.thought;
              const Icon = style.icon;
              const displayText = entry.content || entry.detail || '';
              const displayTitle = entry.title || style.label;
              return (
                <div key={i} style={{
                  display: 'flex', alignItems: 'flex-start', gap: '8px',
                  padding: '6px 8px', borderRadius: '6px',
                  backgroundColor: style.bg,
                  borderLeft: `2px solid ${style.color}`,
                }}>
                  <Icon size={10} color={style.color} style={{ marginTop: '2px', flexShrink: 0 }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {displayTitle && displayTitle !== style.label && (
                      <div style={{ fontSize: '9px', color: style.color, fontWeight: 700, marginBottom: '2px' }}>
                        {displayTitle}
                      </div>
                    )}
                    <div style={{ fontSize: '10px', color: '#cbd5e1', fontFamily: 'var(--font-mono)', wordBreak: 'break-word' }}>
                      {renderTextWithSegLinks(displayText)}
                    </div>
                    {entry.timestamp && (
                      <div style={{ fontSize: '8px', color: '#475569', marginTop: '2px' }}>
                        {new Date(entry.timestamp).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Chat Input */}
          <div style={{
            padding: '8px 12px',
            borderTop: '1px solid #334155',
            backgroundColor: '#0f172a',
            display: 'flex',
            gap: '8px',
            alignItems: 'flex-end',
            flexShrink: 0,
          }}>
            <textarea
              ref={inputRef}
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={chatStreaming ? 'AI is thinking…' : 'Ask Qwen about traffic, routes, diversions…'}
              disabled={chatStreaming}
              rows={1}
              style={{
                flex: 1,
                resize: 'none',
                background: '#1e293b',
                border: '1px solid #334155',
                borderRadius: '8px',
                padding: '8px 12px',
                fontSize: '11px',
                color: 'white',
                fontFamily: 'var(--font-secondary)',
                outline: 'none',
                minHeight: '34px',
                maxHeight: '80px',
                overflowY: 'auto',
                transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
              }}
              onFocus={(e) => { e.target.style.borderColor = '#ea580c'; e.target.style.boxShadow = '0 0 0 2px rgba(234,88,12,0.15)'; }}
              onBlur={(e) => { e.target.style.borderColor = '#334155'; e.target.style.boxShadow = 'none'; }}
              onInput={(e) => {
                e.target.style.height = 'auto';
                e.target.style.height = Math.min(e.target.scrollHeight, 80) + 'px';
              }}
            />
            <button
              onClick={handleSendChat}
              disabled={chatStreaming || !chatInput.trim()}
              style={{
                width: '34px',
                height: '34px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: chatStreaming || !chatInput.trim() ? '#334155' : '#ea580c',
                color: 'white',
                cursor: chatStreaming || !chatInput.trim() ? 'not-allowed' : 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
                transition: 'all 0.2s ease',
                boxShadow: chatStreaming || !chatInput.trim() ? 'none' : '0 2px 8px rgba(234,88,12,0.3)',
              }}
            >
              {chatStreaming ? (
                <div style={{ width: '12px', height: '12px', border: '2px solid #94a3b8', borderTop: '2px solid transparent', borderRadius: '50%', animation: 'spin 1s linear infinite' }} />
              ) : (
                <Send size={14} />
              )}
            </button>
          </div>
        </>
      )}
    </div>
  );
};

export default AIReasoningPanel;
