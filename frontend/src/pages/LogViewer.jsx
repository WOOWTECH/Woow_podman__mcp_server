import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { Search, Pause, Play, ArrowDown, ArrowUp, Trash2, Radio, RefreshCw, AlertTriangle } from 'lucide-react';
import { apiGet, createEventSource, queryString } from '../api';

/**
 * Log page.
 *
 * Two data paths, deliberately:
 *
 * *   `GET /api/logs/stream` is the live tail. It replays the last 200 buffered
 *     lines on connect — nothing older.
 * *   `GET /api/logs/search` reads the whole 5000-line server buffer and does
 *     the level/source/since/regex filtering server-side.
 *
 * The page used to open the stream only and filter its 200-line window in the
 * browser, so investigating anything more than a couple of minutes old showed
 * "Waiting for log entries…" while the server still held the data. Now the
 * list is SEEDED from the search endpoint, and typing a filter switches to a
 * server-side query.
 */

// The server buffer holds this many lines; ask for all of them when seeding.
const BUFFER_LIMIT = 5000;
// Client-side cap, so a long-lived tab cannot grow without bound.
const MAX_CLIENT_LINES = 2000;
const TRIM_TO = 1500;

const LEVELS = ['error', 'warning', 'info', 'debug'];
const SOURCES = ['mcp-server', 'supervisor', 'admin'];
const WINDOWS = [
  { label: 'All buffered', minutes: 0 },
  { label: 'Last 5 min', minutes: 5 },
  { label: 'Last 15 min', minutes: 15 },
  { label: 'Last hour', minutes: 60 },
];

let seq = 0;
/** Parse one raw SSE/search payload into the row shape the list renders. */
function toEntry(raw) {
  seq += 1;
  let data = {};
  try {
    data = JSON.parse(raw);
  } catch {
    data = {};
  }
  return {
    id: `${Date.now()}-${seq}`,
    raw,
    timestamp: data.timestamp || new Date().toISOString(),
    level: data.level || 'info',
    message: data.message ?? raw,
    source: data.source || '',
  };
}

export default function LogViewer() {
  const [logs, setLogs] = useState([]);
  const [results, setResults] = useState(null); // server-search rows, or null
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState(null);
  const [filter, setFilter] = useState('');
  const [level, setLevel] = useState('');
  const [source, setSource] = useState('');
  const [windowMinutes, setWindowMinutes] = useState(0);
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [connected, setConnected] = useState(false);
  const [seeded, setSeeded] = useState(false);
  const containerRef = useRef(null);
  const pausedLogsRef = useRef([]);

  // Replay reconciliation state: the stream re-sends the last 200 buffered
  // lines, which the seed already contains. Without this the page opened with
  // every recent line printed twice.
  const seedRawRef = useRef([]);
  const replayActiveRef = useRef(true);
  const replayCursorRef = useRef(null);

  const searchActive = !!(filter || level || source || windowMinutes);

  const addLog = useCallback(
    (entry) => {
      if (paused) {
        pausedLogsRef.current.push(entry);
        return;
      }
      setLogs((prev) => {
        const updated = [...prev, entry];
        return updated.length > MAX_CLIENT_LINES ? updated.slice(-TRIM_TO) : updated;
      });
    },
    [paused]
  );

  // Seed from the full server buffer before the stream opens.
  useEffect(() => {
    let cancelled = false;
    apiGet(`/logs/search${queryString({ limit: BUFFER_LIMIT })}`)
      .then((data) => {
        if (cancelled) return;
        const lines = Array.isArray(data?.lines) ? data.lines : [];
        seedRawRef.current = lines;
        setLogs(lines.map(toEntry));
      })
      .catch(() => {
        // A failed seed is not fatal — the stream still delivers the last 200.
        if (!cancelled) seedRawRef.current = [];
      })
      .finally(() => {
        if (!cancelled) setSeeded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Drop a streamed line that the seed already showed.
   *
   * The replay is a contiguous suffix of the buffer, so once the first
   * incoming line is located in the seed the rest follow positionally.
   */
  const isReplayDuplicate = useCallback((raw) => {
    if (!replayActiveRef.current) return false;
    const seedLines = seedRawRef.current;
    if (replayCursorRef.current === null) {
      const index = seedLines.lastIndexOf(raw);
      if (index === -1) {
        replayActiveRef.current = false;
        return false;
      }
      replayCursorRef.current = index + 1;
      return true;
    }
    const cursor = replayCursorRef.current;
    if (cursor < seedLines.length && seedLines[cursor] === raw) {
      replayCursorRef.current = cursor + 1;
      return true;
    }
    replayActiveRef.current = false;
    return false;
  }, []);

  useEffect(() => {
    // Wait for the seed so the replay window can be reconciled against it.
    if (!seeded) return undefined;
    const es = createEventSource('/logs/stream');

    es.onopen = () => setConnected(true);

    es.onmessage = (event) => {
      if (isReplayDuplicate(event.data)) return;
      addLog(toEntry(event.data));
    };

    es.onerror = () => setConnected(false);

    return () => {
      es.close();
    };
  }, [seeded, addLog, isReplayDuplicate]);

  useEffect(() => {
    if (!paused && pausedLogsRef.current.length > 0) {
      setLogs((prev) => {
        const combined = [...prev, ...pausedLogsRef.current];
        pausedLogsRef.current = [];
        return combined.length > MAX_CLIENT_LINES ? combined.slice(-TRIM_TO) : combined;
      });
    }
  }, [paused]);

  const runSearch = useCallback(() => {
    if (!searchActive) {
      setResults(null);
      setSearchError(null);
      return;
    }
    const since = windowMinutes
      ? new Date(Date.now() - windowMinutes * 60_000).toISOString()
      : '';
    setSearching(true);
    apiGet(
      `/logs/search${queryString({
        q: filter,
        level,
        source,
        since,
        limit: BUFFER_LIMIT,
      })}`
    )
      .then((data) => {
        const lines = Array.isArray(data?.lines) ? data.lines : [];
        setResults(lines.map(toEntry));
        setSearchError(null);
      })
      .catch((err) => {
        setResults([]);
        setSearchError(err.message);
      })
      .finally(() => setSearching(false));
  }, [searchActive, filter, level, source, windowMinutes]);

  // Debounce the query so typing does not fire one request per keystroke.
  useEffect(() => {
    if (!searchActive) {
      setResults(null);
      setSearchError(null);
      return undefined;
    }
    const timer = setTimeout(runSearch, 300);
    return () => clearTimeout(timer);
  }, [searchActive, runSearch]);

  // A server-side search is a snapshot; refresh it while it is on screen so the
  // page does not quietly stop being a log *tail* the moment a filter is typed.
  useEffect(() => {
    if (!searchActive || paused) return undefined;
    const timer = setInterval(runSearch, 5000);
    return () => clearInterval(timer);
  }, [searchActive, paused, runSearch]);

  // Memoised so it keeps its identity between renders: the auto-scroll effect
  // below depends on it, and a fresh array every render re-ran that effect (and
  // yanked the viewport back to the bottom) on every unrelated state change.
  const rows = useMemo(
    () => (searchActive ? results || [] : logs),
    [searchActive, results, logs]
  );

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [rows, autoScroll]);

  const counts = useMemo(
    () => ({ shown: rows.length, buffered: logs.length }),
    [rows.length, logs.length]
  );

  function getLevelColor(value) {
    switch (String(value).toLowerCase()) {
      case 'error':
      case 'critical':
        return 'text-red-400';
      case 'warn':
      case 'warning':
        return 'text-yellow-400';
      case 'debug':
        return 'text-gray-500';
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

  function clearFilters() {
    setFilter('');
    setLevel('');
    setSource('');
    setWindowMinutes(0);
  }

  const selectClass =
    'px-2.5 py-2 bg-gray-900 border border-gray-800 rounded-lg text-gray-300 text-sm focus:outline-none focus:border-brand-500';

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)]">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-2xl font-bold text-gray-100">Log Viewer</h2>
          <div className="flex items-center gap-2 mt-1">
            <div
              className={`w-2 h-2 rounded-full ${
                connected ? 'bg-emerald-500 animate-pulse' : 'bg-red-500'
              }`}
            />
            <span className="text-sm text-gray-500">
              {connected ? 'Live' : 'Stream disconnected'} &middot; {counts.shown} shown
              {searchActive && <span> (server search of the last {BUFFER_LIMIT} lines)</span>}
              {!searchActive && <span> &middot; {counts.buffered} buffered</span>}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => setPaused(!paused)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
              paused
                ? 'bg-amber-600/20 text-amber-400 border border-amber-600/30'
                : 'bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700'
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
                : 'bg-gray-800 text-gray-400 hover:text-gray-200 hover:bg-gray-700'
            }`}
            title={autoScroll ? 'Disable auto-scroll' : 'Enable auto-scroll'}
          >
            {autoScroll ? <ArrowDown size={14} /> : <ArrowUp size={14} />}
            <span>Auto-scroll</span>
          </button>

          <button
            onClick={() => (searchActive ? runSearch() : setLogs([]))}
            className="flex items-center gap-1.5 px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-gray-200 rounded-lg text-sm font-medium transition-colors"
            title={searchActive ? 'Re-run the search' : 'Clear the view (the server buffer is untouched)'}
          >
            {searchActive ? <RefreshCw size={14} className={searching ? 'animate-spin' : ''} /> : <Trash2 size={14} />}
            <span>{searchActive ? 'Refresh' : 'Clear'}</span>
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <div className="relative flex-1 min-w-[220px]">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search the whole server buffer…"
            className="w-full pl-9 pr-4 py-2 bg-gray-900 border border-gray-800 rounded-lg text-gray-200 placeholder-gray-600 text-sm focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors"
          />
        </div>

        <select value={level} onChange={(e) => setLevel(e.target.value)} className={selectClass}>
          <option value="">All levels</option>
          {LEVELS.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>

        <select value={source} onChange={(e) => setSource(e.target.value)} className={selectClass}>
          <option value="">All sources</option>
          {SOURCES.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>

        <select
          value={windowMinutes}
          onChange={(e) => setWindowMinutes(Number(e.target.value))}
          className={selectClass}
        >
          {WINDOWS.map((option) => (
            <option key={option.minutes} value={option.minutes}>
              {option.label}
            </option>
          ))}
        </select>

        {searchActive && (
          <button
            onClick={clearFilters}
            className="px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-gray-200 rounded-lg text-sm transition-colors"
          >
            Clear filters
          </button>
        )}
      </div>

      {searchError && (
        <div className="flex items-start gap-2 px-3 py-2.5 mb-3 bg-red-500/10 border border-red-500/20 rounded-lg text-sm text-red-400">
          <AlertTriangle size={16} className="shrink-0 mt-0.5" />
          <span>Search failed: {searchError}</span>
        </div>
      )}

      {!connected && seeded && (
        <div className="flex items-start gap-2 px-3 py-2.5 mb-3 bg-amber-500/10 border border-amber-500/20 rounded-lg text-sm text-amber-300">
          <AlertTriangle size={16} className="shrink-0 mt-0.5" />
          <span>
            The live stream is not connected, so new lines will not appear on their own. The
            stream authenticates with the session cookie set at login — if you have been signed
            out, log in again. Search still reads the server buffer.
          </span>
        </div>
      )}

      <div
        ref={containerRef}
        className="flex-1 bg-gray-950 border border-gray-800 rounded-xl overflow-y-auto font-mono text-xs leading-relaxed"
      >
        {rows.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-gray-600">
            <Radio size={24} className="mb-2 opacity-50" />
            <p>
              {searchActive
                ? searching
                  ? 'Searching…'
                  : 'No buffered line matches these filters.'
                : connected
                  ? 'Waiting for log entries...'
                  : 'Connecting to log stream...'}
            </p>
          </div>
        ) : (
          <div className="p-3 space-y-0.5">
            {rows.map((log) => (
              <div key={log.id} className="flex gap-2 hover:bg-gray-900/50 px-1 py-0.5 rounded">
                <span className="text-gray-600 shrink-0 select-none">
                  {formatTimestamp(log.timestamp)}
                </span>
                <span className={`shrink-0 uppercase w-12 text-right ${getLevelColor(log.level)}`}>
                  {String(log.level).substring(0, 5).padEnd(5)}
                </span>
                {log.source && <span className="text-gray-500 shrink-0">[{log.source}]</span>}
                <span className="text-gray-300 break-all">{log.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
