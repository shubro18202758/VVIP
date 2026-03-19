import React, { useState, useRef, useEffect, useMemo } from 'react';
import { ArrowLeft, Send, Shield, Radio, MessageSquare, MoreVertical, Terminal, Brain, Activity, ChevronDown, ChevronRight, Cpu, AlertTriangle, CheckCircle, Clock } from 'lucide-react';
import { useConvoy } from '../context/ConvoyContext';
import * as api from '../services/api';

const TOOL_ICONS = {
  predict_traffic_flow: Activity,
  find_convoy_routes: Activity,
  plan_diversions: AlertTriangle,
  evaluate_scenarios: Brain,
  predict_eta: Clock,
  get_live_traffic: Activity,
  default: Terminal,
};

const InterDeptComms = ({ navigate }) => {
  const { lifecycle, movementId, planResult, addReasoning, gpuHealth, backendHealth, corridorSummary } = useConvoy();

  const [activeChannel, setActiveChannel] = useState('Tactical-1');
  const [messages, setMessages] = useState([
    {
      id: 1,
      sender: 'Qwen 3.5 Agent',
      dept: 'Convoy Brain',
      text: 'Orchestrator online. Qwen 3.5 9B (Q4_K_M) loaded via Ollama. Standing by for tactical commands.',
      time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }),
      type: 'assistant',
      cot: [],
      toolResults: [],
    },
  ]);
  const [inputValue, setInputValue] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [expandedCot, setExpandedCot] = useState({});
  const chatEndRef = useRef(null);
  const inputRef = useRef(null);

  // Dynamic channel list with live movement context
  const channels = useMemo(() => {
    const mvCount = corridorSummary?.active_movements || 0;
    const anomCount = corridorSummary?.anomaly_count || 0;
    return [
      { id: 'Tactical-1', name: 'Convoy Tactical Ops', members: mvCount > 0 ? mvCount + 4 : 4, status: lifecycle === 'active' ? 'Active' : 'Standby', icon: 'shield' },
      { id: 'Traffic-A', name: 'Traffic Intelligence', members: corridorSummary?.total_segments || 0, status: anomCount > 0 ? 'Alert' : 'Clear', icon: 'activity' },
      { id: 'Intelligence', name: 'AI Reasoning Feed', members: 1, status: 'Secured', icon: 'brain' },
      { id: 'Diagnostics', name: 'System Diagnostics', members: 3, status: gpuHealth ? 'Online' : 'Offline', icon: 'cpu' },
    ];
  }, [lifecycle, corridorSummary, gpuHealth]);

  // Auto-scroll on new messages
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = async () => {
    if (!inputValue.trim() || isStreaming) return;
    const userMsg = inputValue;
    setInputValue('');

    const newMsgId = Date.now();
    setMessages((prev) => [
      ...prev,
      {
        id: newMsgId,
        sender: 'Commander',
        dept: 'HQ',
        text: userMsg,
        time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }),
        type: 'user',
      },
    ]);

    setTimeout(() => streamResponse(userMsg), 100);
  };

  const streamResponse = async (prompt) => {
    setIsStreaming(true);
    const asstId = Date.now() + 1;

    setMessages((prev) => [
      ...prev,
      {
        id: asstId,
        sender: 'Qwen 3.5 Agent',
        dept: 'Convoy Brain',
        text: '',
        time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }),
        type: 'assistant',
        cot: [],
        toolResults: [],
      },
    ]);

    try {
      const response = await fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: prompt,
          movement_id: movementId || undefined,
          vvip_class: planResult?.vvip_class || 'Z',
        }),
      });

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);

            setMessages((prev) =>
              prev.map((msg) => {
                if (msg.id !== asstId) return msg;
                const newMsg = { ...msg };

                if (event.type === 'token') {
                  newMsg.text += event.data;
                } else if (event.type === 'thought') {
                  newMsg.cot = [...newMsg.cot, { type: 'thought', text: event.data?.text || event.data, stepIndex: event.data?.stepIndex }];
                  addReasoning({ type: 'thought', content: event.data?.text || event.data });
                } else if (event.type === 'tool_call') {
                  newMsg.cot = [
                    ...newMsg.cot,
                    {
                      type: 'tool_call',
                      callId: event.data.callId,
                      toolName: event.data.toolName,
                      arguments: event.data.arguments,
                      state: event.data.state,
                    },
                  ];
                  addReasoning({ type: 'tool', content: `Calling ${event.data.toolName}` });
                } else if (event.type === 'tool_result') {
                  newMsg.cot = [
                    ...newMsg.cot,
                    {
                      type: 'tool_result',
                      callId: event.data.callId,
                      state: event.data.state,
                      result: event.data.result,
                      durationMs: event.data.durationMs,
                    },
                  ];
                  newMsg.toolResults = [...(newMsg.toolResults || []), event.data];
                  addReasoning({ type: 'tool', content: `${event.data.callId} → ${event.data.state} (${event.data.durationMs}ms)` });
                } else if (event.type === 'error') {
                  newMsg.cot = [...newMsg.cot, { type: 'error', text: event.data }];
                  addReasoning({ type: 'error', content: event.data });
                }

                return newMsg;
              })
            );
          } catch {
            // NDJSON parse error — skip malformed line
          }
        }
      }
    } catch (err) {
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === asstId ? { ...msg, text: msg.text || `Stream error: ${err.message}` } : msg
        )
      );
    } finally {
      setIsStreaming(false);
      inputRef.current?.focus();
    }
  };

  const toggleCot = (msgId) => setExpandedCot((prev) => ({ ...prev, [msgId]: !prev[msgId] }));

  const channelIcon = (icon) => {
    switch (icon) {
      case 'shield': return <Shield size={14} color="white" />;
      case 'activity': return <Activity size={14} color="white" />;
      case 'brain': return <Brain size={14} color="white" />;
      case 'cpu': return <Cpu size={14} color="white" />;
      default: return <MessageSquare size={14} color="white" />;
    }
  };

  const statusColor = (status) => {
    if (status === 'Active' || status === 'Online' || status === 'Clear') return '#16a34a';
    if (status === 'Alert') return '#ef4444';
    if (status === 'Secured') return '#3b82f6';
    return '#94a3b8';
  };

  return (
    <div style={{ height: '100vh', width: '100vw', backgroundColor: '#0f172a', display: 'flex', flexDirection: 'column', color: '#e2e8f0' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 20px', backgroundColor: '#1e293b', borderBottom: '1px solid #334155', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <button onClick={() => navigate('/')} style={{ width: '34px', height: '34px', borderRadius: '8px', backgroundColor: '#334155', border: 'none', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', color: '#94a3b8' }}>
            <ArrowLeft size={16} />
          </button>
          <div>
            <h1 style={{ fontSize: '16px', fontWeight: 700, margin: 0, color: 'white' }}>Inter-Department Comms</h1>
            <p style={{ fontSize: '11px', color: '#94a3b8', margin: 0 }}>Qwen 3.5 9B · NDJSON Stream · {lifecycle !== 'idle' ? lifecycle.toUpperCase() : 'STANDBY'}</p>
          </div>
        </div>

        <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
          {gpuHealth && (
            <div style={{ fontSize: '10px', color: '#94a3b8', fontFamily: 'var(--font-mono)', padding: '4px 10px', backgroundColor: '#0f172a', borderRadius: '8px', border: '1px solid #334155' }}>
              VRAM {gpuHealth.vram_used_mb}/{gpuHealth.vram_total_mb}MB · {gpuHealth.temperature}°C
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '4px 10px', backgroundColor: '#0f172a', borderRadius: '8px', border: '1px solid #334155' }}>
            <Radio size={12} color="#ef4444" className="animate-pulse" />
            <span style={{ fontSize: '10px', fontWeight: 700, color: '#ef4444' }}>LIVE</span>
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Sidebar: Channels */}
        <div style={{ width: '260px', backgroundColor: '#1e293b', borderRight: '1px solid #334155', display: 'flex', flexDirection: 'column' }}>
          <div style={{ padding: '16px' }}>
            <h4 style={{ fontSize: '10px', fontWeight: 800, color: '#64748b', textTransform: 'uppercase', marginBottom: '12px', letterSpacing: '0.05em' }}>Operational Channels</h4>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
              {channels.map((ch) => (
                <div
                  key={ch.id}
                  onClick={() => setActiveChannel(ch.id)}
                  style={{ padding: '10px', borderRadius: '8px', backgroundColor: activeChannel === ch.id ? '#334155' : 'transparent', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '10px', transition: 'background-color 0.15s' }}
                >
                  <div style={{ width: '30px', height: '30px', borderRadius: '6px', backgroundColor: activeChannel === ch.id ? '#ea580c' : '#0f172a', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    {channelIcon(ch.icon)}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: '12px', fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{ch.name}</div>
                    <div style={{ fontSize: '9px', color: '#94a3b8', display: 'flex', justifyContent: 'space-between' }}>
                      <span>{ch.members} nodes</span>
                      <span style={{ color: statusColor(ch.status), fontWeight: 600 }}>{ch.status}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* System Status Sidebar */}
          <div style={{ marginTop: 'auto', padding: '14px', borderTop: '1px solid #334155' }}>
            <div style={{ padding: '10px', backgroundColor: '#0f172a', borderRadius: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px' }}>
                <span style={{ color: '#94a3b8' }}>Ollama</span>
                <span style={{ color: backendHealth?.ollama === 'connected' ? '#16a34a' : '#ef4444', fontWeight: 700 }}>
                  {backendHealth?.ollama === 'connected' ? '● UP' : '● DOWN'}
                </span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px' }}>
                <span style={{ color: '#94a3b8' }}>Brain</span>
                <span style={{ color: backendHealth?.status === 'ok' ? '#16a34a' : '#eab308', fontWeight: 700 }}>
                  {backendHealth?.status === 'ok' ? '● OK' : '● DEGRADED'}
                </span>
              </div>
              {corridorSummary && (
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px' }}>
                  <span style={{ color: '#94a3b8' }}>Congestion</span>
                  <span style={{ color: corridorSummary.avg_congestion_idx > 0.5 ? '#ef4444' : '#16a34a', fontWeight: 700, fontFamily: 'var(--font-mono)' }}>
                    {corridorSummary.avg_congestion_idx?.toFixed(3) || '—'}
                  </span>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Chat Area */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', backgroundColor: '#0f172a' }}>
          <div style={{ flex: 1, padding: '16px 20px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {messages.map((msg) => (
              <div key={msg.id} style={{ maxWidth: '85%', alignSelf: msg.type === 'user' ? 'flex-end' : 'flex-start', display: 'flex', flexDirection: 'column', gap: '3px' }}>
                {/* Sender line */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '2px', justifyContent: msg.type === 'user' ? 'flex-end' : 'flex-start' }}>
                  <span style={{ fontSize: '10px', fontWeight: 700, color: msg.type === 'user' ? '#94a3b8' : '#ea580c' }}>{msg.sender}</span>
                  <span style={{ fontSize: '8px', padding: '1px 5px', backgroundColor: '#1e293b', borderRadius: '3px', color: '#64748b' }}>{msg.dept}</span>
                  <span style={{ fontSize: '9px', color: '#475569' }}>{msg.time}</span>
                </div>

                {/* Message bubble */}
                <div style={{
                  padding: '10px 14px',
                  borderRadius: msg.type === 'user' ? '12px 12px 2px 12px' : '12px 12px 12px 2px',
                  backgroundColor: msg.type === 'user' ? '#ea580c' : '#1e293b',
                  color: msg.type === 'user' ? 'white' : '#e2e8f0',
                  fontSize: '12px',
                  lineHeight: 1.5,
                  whiteSpace: 'pre-wrap',
                }}>
                  {/* CoT Section — collapsible */}
                  {msg.cot && msg.cot.length > 0 && (
                    <div style={{ marginBottom: '8px' }}>
                      <div
                        onClick={() => toggleCot(msg.id)}
                        style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer', fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 800, marginBottom: '6px', userSelect: 'none' }}
                      >
                        {expandedCot[msg.id] ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
                        Chain of Thought ({msg.cot.length} steps)
                      </div>
                      {expandedCot[msg.id] && (
                        <div style={{ padding: '8px', backgroundColor: '#0f172a', borderRadius: '6px', borderLeft: '2px solid #475569', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                          {msg.cot.map((step, idx) => {
                            if (step.type === 'thought') {
                              return (
                                <div key={idx} style={{ fontSize: '10px', color: '#cbd5e1', fontFamily: 'var(--font-mono)' }}>
                                  {step.stepIndex != null ? `[${step.stepIndex}] ` : '· '}{step.text}
                                </div>
                              );
                            }
                            if (step.type === 'tool_call') {
                              const ToolIcon = TOOL_ICONS[step.toolName] || TOOL_ICONS.default;
                              return (
                                <div key={idx} style={{ fontSize: '10px', color: '#3b82f6', fontFamily: 'var(--font-mono)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                                  <ToolIcon size={10} /> {step.toolName}({Object.keys(step.arguments || {}).join(', ')})
                                  <span style={{ color: '#475569', fontSize: '8px', marginLeft: 'auto' }}>{step.state}</span>
                                </div>
                              );
                            }
                            if (step.type === 'tool_result') {
                              return (
                                <div key={idx} style={{ fontSize: '10px', color: step.state === 'success' ? '#16a34a' : '#ef4444', fontFamily: 'var(--font-mono)' }}>
                                  ← {step.callId} · {step.state} · {step.durationMs}ms
                                </div>
                              );
                            }
                            if (step.type === 'error') {
                              return (
                                <div key={idx} style={{ fontSize: '10px', color: '#ef4444', fontFamily: 'var(--font-mono)' }}>
                                  ✗ {step.text}
                                </div>
                              );
                            }
                            return null;
                          })}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Tool Result Previews */}
                  {msg.toolResults && msg.toolResults.length > 0 && (
                    <div style={{ marginBottom: '8px', display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                      {msg.toolResults.map((tr, i) => (
                        <div key={i} style={{
                          padding: '4px 8px',
                          borderRadius: '4px',
                          backgroundColor: tr.state === 'success' ? 'rgba(22,163,74,0.1)' : 'rgba(239,68,68,0.1)',
                          border: `1px solid ${tr.state === 'success' ? '#16a34a33' : '#ef444433'}`,
                          fontSize: '9px',
                          fontFamily: 'var(--font-mono)',
                          color: tr.state === 'success' ? '#16a34a' : '#ef4444',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '4px',
                        }}>
                          {tr.state === 'success' ? <CheckCircle size={8} /> : <AlertTriangle size={8} />}
                          {tr.callId?.slice(0, 12)} · {tr.durationMs}ms
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Message text */}
                  {msg.text || (isStreaming && msg.id === messages[messages.length - 1].id ? <span className="animate-pulse" style={{ color: '#ea580c' }}>▊</span> : '')}
                </div>
              </div>
            ))}
            <div ref={chatEndRef} />
          </div>

          {/* Input Area */}
          <div style={{ padding: '14px 20px', backgroundColor: '#1e293b', borderTop: '1px solid #334155', flexShrink: 0 }}>
            {lifecycle !== 'idle' && (
              <div style={{ fontSize: '9px', color: '#64748b', marginBottom: '6px', fontFamily: 'var(--font-mono)' }}>
                Movement: {movementId?.slice(0, 8) || '—'} · Lifecycle: {lifecycle} · Class: {planResult?.vvip_class || 'N/A'}
              </div>
            )}
            <div style={{ position: 'relative' }}>
              <input
                ref={inputRef}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSend()}
                placeholder={isStreaming ? 'Qwen is thinking...' : 'Type your command to the AI Orchestrator...'}
                disabled={isStreaming}
                style={{
                  width: '100%',
                  padding: '12px 14px',
                  paddingRight: '110px',
                  backgroundColor: '#0f172a',
                  border: `1px solid ${isStreaming ? '#ea580c44' : '#334155'}`,
                  borderRadius: '10px',
                  color: 'white',
                  fontSize: '13px',
                  outline: 'none',
                  transition: 'border-color 0.15s',
                  opacity: isStreaming ? 0.7 : 1,
                }}
              />
              <div style={{ position: 'absolute', right: '6px', top: '50%', transform: 'translateY(-50%)', display: 'flex', gap: '6px' }}>
                <button
                  onClick={handleSend}
                  disabled={isStreaming || !inputValue.trim()}
                  style={{
                    backgroundColor: isStreaming ? '#475569' : '#ea580c',
                    border: 'none',
                    color: 'white',
                    padding: '7px 14px',
                    borderRadius: '6px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    fontWeight: 700,
                    fontSize: '12px',
                    cursor: isStreaming ? 'not-allowed' : 'pointer',
                  }}
                >
                  <Send size={12} /> {isStreaming ? 'Streaming...' : 'Send'}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default InterDeptComms;
