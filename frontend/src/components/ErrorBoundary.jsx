import React from 'react';
import { AlertTriangle, RotateCw } from 'lucide-react';

/**
 * Catch a render-time crash in one page instead of unmounting the whole SPA.
 *
 * React 18/19 unmount the entire root when a render throws and nothing catches
 * it. The console had no boundary at all, so a single bad value — e.g. a token
 * history entry handed to JSX as a raw object ("Objects are not valid as a
 * React child") — blanked every page, and the operator's instinctive reload
 * landed back on the same route and blanked it again. The boundary keeps the
 * sidebar usable and offers a way out.
 *
 * `resetKey` is the current path: navigating elsewhere clears the error, so a
 * broken page never traps the session.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Keep the stack reachable in devtools; the banner only shows the message.
    console.error('Admin console render error:', error, info);
  }

  componentDidUpdate(prevProps) {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="bg-red-500/10 border border-red-500/20 rounded-xl p-6 max-w-2xl">
        <div className="flex items-start gap-3">
          <AlertTriangle size={20} className="text-red-400 mt-0.5 shrink-0" />
          <div className="min-w-0">
            <p className="text-red-400 font-medium">This page failed to render</p>
            <p className="text-red-400/70 text-sm mt-1">
              The rest of the console still works — pick another page in the sidebar, or
              reload to try again.
            </p>
            <pre className="mt-3 p-3 bg-gray-950 border border-gray-800 rounded-lg text-xs text-red-300 whitespace-pre-wrap break-words">
              {String((error && error.message) || error)}
            </pre>
            <div className="flex gap-2 mt-4">
              <button
                onClick={() => this.setState({ error: null })}
                className="flex items-center gap-1.5 px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm transition-colors"
              >
                <RotateCw size={14} />
                <span>Try again</span>
              </button>
              <button
                onClick={() => window.location.reload()}
                className="px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm transition-colors"
              >
                Reload console
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }
}
