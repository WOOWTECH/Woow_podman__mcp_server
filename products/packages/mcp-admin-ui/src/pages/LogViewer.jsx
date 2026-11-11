import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Search, Pause, Play, ArrowDown, ArrowUp, Trash2, Radio, RefreshCw } from 'lucide-react';
import { createEventSource } from '../api';

export default function LogViewer() {
  const [logs, setLogs] = useState([]);
  const [filter, setFilter] = useState('');
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [connected, setConnected] = useState(false);
  const containerRef = useRef(null);
  const eventSourceRef = useRef(null);
  const pausedLogsRef = useRef([]);
  const pausedRef = useRef(false);

  // Keep the ref in sync with state so the SSE callback always sees
  // the latest paused value without needing to be in the dependency array.
  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  const addLog = useCallback((entry) => {
    if (pausedRef.current) {
      pausedLogsRef.current.push(entry);
      return;
    }
    setLogs((prev) => {
      const updated = [...prev, entry];
      if (updated.length > 2000) {
        return updated.slice(-1500);
      }
      return updated;
    });
  }, []); // stable reference -- reads pausedRef instead of paused

  const connectSSE = useCallback(() => {
    // Close existing connection if any
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    setConnected(false);

    const es = createEventSource('/logs/stream');
    eventSourceRef.current = es;

    // Listen for the named "connected" event sent by the backend
    es.addEventListener('connected', () => {
      setConnected(true);
    });

    // Also set connected on generic open (backup)
    es.onopen = () => {
      setConnected(true);
    };

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // Skip heartbeat / error-only payloads
        if (data.error) {
          console.warn('Log stream error:', data.error);
          return;
        }
        addLog({
          id: Date.now() + Math.random(),
          timestamp: data.timestamp || new Date().toISOString(),
          level: data.level || 'info',
          message: data.message || event.data,
          source: data.source || '',
        });
      } catch {
        addLog({
          id: Date.now() + Math.random(),
          timestamp: new Date().toISOString(),
          level: 'info',
          message: event.data,
          source: '',
        });
      }
    };

    es.onerror = () => {
      setConnected(false);
      // EventSource auto-reconnects on error. If it is CLOSED (readyState === 2)
      // it means reconnection has given up.
    };
  }, [addLog]);

  // Open SSE connection on mount; clean up on unmount
  useEffect(() => {
    connectSSE();

    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
    };
  }, [connectSSE]);

  // Flush buffered logs when un-pausing
  useEffect(() => {
    if (!paused && pausedLogsRef.current.length > 0) {
      setLogs((prev) => {
        const combined = [...prev, ...pausedLogsRef.current];
        pausedLogsRef.current = [];
        if (combined.length > 2000) {
          return combined.slice(-1500);
        }
        return combined;
      });
    }
  }, [paused]);

  // Auto-scroll when new logs arrive
  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  const filteredLogs = filter
    ? logs.filter(
        (l) =>
          l.message.toLowerCase().includes(filter.toLowerCase()) ||
          l.source.toLowerCase().includes(filter.toLowerCase()) ||
          l.level.toLowerCase().includes(filter.toLowerCase())
      )
    : logs;

  function getLevelColor(level) {
    switch (level.toLowerCase()) {
      case 'error':
        return 'text-red-600';
      case 'warn':
      case 'warning':
        return 'text-yellow-400';
      case 'debug':
        return 'text-muted';
      default:
        return 'text-blue-400';
    }
  }

  function formatTimestamp(ts) {
    try {
      return new Date(ts).toLocaleTimeString('en-US', { hour12: false, fractionalSecondDigits: 3 });
    } catch {
      return ts;
    }
  }

  function handleReconnect() {
    connectSSE();
  }

  return (
    <div className="flex flex-col h-[calc(100vh-10rem)] lg:h-[calc(100vh-8rem)]">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
        <div>
          <h2 className="text-xl sm:text-2xl font-bold text-ink">Log Viewer</h2>
          <div className="flex items-center gap-2 mt-1">
            <div
              className={`w-2 h-2 rounded-full ${
                connected ? 'bg-emerald-500 animate-pulse' : 'bg-red-500'
              }`}
            />
            <span className="text-sm text-muted">
              {connected ? 'Connected' : 'Disconnected'} &middot; {logs.length} lines
            </span>
            {!connected && (
              <button
                onClick={handleReconnect}
                className="flex items-center gap-1 text-xs text-brand-400 hover:text-brand-300 transition-colors"
                title="Reconnect"
              >
                <RefreshCw size={12} />
                <span>Reconnect</span>
              </button>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={() => setPaused(!paused)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
              paused
                ? 'bg-amber-600/20 text-amber-600 border border-amber-600/30'
                : 'bg-panel text-muted hover:text-ink-2 hover:bg-field'
            }`}
            title={paused ? 'Resume' : 'Pause'}
          >
            {paused ? <Play size={14} /> : <Pause size={14} />}
            <span>{paused ? 'Resume' : 'Pause'}</span>
            {paused && pausedLogsRef.current.length > 0 && (
              <span className="ml-1 px-1.5 py-0.5 bg-amber-600/30 rounded text-xs">
                +{pausedLogsRef.current.length}
              </span>
            )}
          </button>

          <button
            onClick={() => setAutoScroll(!autoScroll)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
              autoScroll
                ? 'bg-brand-600/20 text-brand-400 border border-brand-600/30'
                : 'bg-panel text-muted hover:text-ink-2 hover:bg-field'
            }`}
            title={autoScroll ? 'Disable auto-scroll' : 'Enable auto-scroll'}
          >
            {autoScroll ? <ArrowDown size={14} /> : <ArrowUp size={14} />}
            <span>Auto-scroll</span>
          </button>

          <button
            onClick={() => setLogs([])}
            className="flex items-center gap-1.5 px-3 py-2 bg-panel hover:bg-field text-muted hover:text-ink-2 rounded-lg text-sm font-medium transition-colors"
            title="Clear logs"
          >
            <Trash2 size={14} />
            <span>Clear</span>
          </button>
        </div>
      </div>

      <div className="relative mb-3">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
        <input
          type="text"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter logs..."
          className="w-full pl-9 pr-4 py-2 bg-bg border border-border rounded-lg text-ink-2 placeholder-muted-2 text-sm focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors"
        />
      </div>

      <div
        ref={containerRef}
        className="flex-1 bg-bg border border-border rounded-xl overflow-y-auto font-mono text-xs leading-relaxed"
      >
        {filteredLogs.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-muted-2">
            <Radio size={24} className="mb-2 opacity-50" />
            <p>{connected ? 'Waiting for log entries...' : 'Connecting to log stream...'}</p>
          </div>
        ) : (
          <div className="p-3 space-y-0.5">
            {filteredLogs.map((log) => (
              <div key={log.id} className="flex gap-2 hover:bg-bg/50 px-1 py-0.5 rounded">
                <span className="text-muted-2 shrink-0 select-none">
                  {formatTimestamp(log.timestamp)}
                </span>
                <span className={`shrink-0 uppercase w-12 text-right ${getLevelColor(log.level)}`}>
                  {log.level.substring(0, 5).padEnd(5)}
                </span>
                {log.source && (
                  <span className="text-muted shrink-0">[{log.source}]</span>
                )}
                <span className="text-ink-2 break-all">{log.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
