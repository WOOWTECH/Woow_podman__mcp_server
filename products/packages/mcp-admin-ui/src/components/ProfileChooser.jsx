import React, { useState } from 'react';
import { DEFAULT_PROFILE_ID } from '../api';

export default function ProfileChooser({ profiles, selectedId, onSelect, onCreate, onRename, onDelete }) {
  const selected = profiles.find(profile => profile.id === selectedId);
  const [action, setAction] = useState(null);
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  function begin(nextAction) {
    setAction(nextAction);
    setLabel(nextAction === 'rename' ? selected?.label || '' : '');
    setError('');
  }

  async function submit(event) {
    event.preventDefault();
    const trimmed = label.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError('');
    try {
      if (action === 'create') await onCreate(trimmed);
      if (action === 'rename') await onRename(selectedId, trimmed);
      setAction(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function confirmDelete() {
    if (busy || selected?.is_default || selectedId === DEFAULT_PROFILE_ID) return;
    setBusy(true);
    setError('');
    try {
      await onDelete(selectedId);
      setAction(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mb-5 rounded-lg border border-border bg-panel p-3 max-w-xl lg:ml-auto" aria-label="Profiles">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="mcp-profile-select" className="text-sm font-medium text-ink-2">Profile</label>
        <select
          id="mcp-profile-select"
          value={selectedId}
          disabled={busy}
          onChange={event => onSelect(event.target.value)}
          className="min-w-0 flex-1 rounded-lg border border-border bg-bg px-3 py-2 text-sm text-ink"
        >
          {profiles.map(profile => (
            <option key={profile.id} value={profile.id}>
              {profile.label}
            </option>
          ))}
        </select>
        <button type="button" disabled={busy} onClick={() => begin('create')} className="text-sm text-brand-400 hover:text-brand-300 disabled:opacity-50">Create</button>
        <button type="button" disabled={busy || selected?.is_default || selectedId === DEFAULT_PROFILE_ID} onClick={() => begin('rename')} className="text-sm text-brand-400 hover:text-brand-300 disabled:opacity-50">Rename</button>
        <button type="button" disabled={busy || selected?.is_default || selectedId === DEFAULT_PROFILE_ID} onClick={() => begin('delete')} className="text-sm text-red-600 hover:text-red-500 disabled:opacity-50">Delete</button>
      </div>

      {action === 'delete' && (
        <div className="mt-3 border-t border-border pt-3 text-sm text-ink-2">
          <p>Delete {selected?.label}? This removes the profile and its configuration.</p>
          <div className="mt-2 flex gap-3">
            <button type="button" disabled={busy} onClick={confirmDelete} className="rounded-lg bg-red-600 px-3 py-1.5 text-white disabled:opacity-50">Confirm delete profile</button>
            <button type="button" disabled={busy} onClick={() => begin(null)} className="text-muted hover:text-ink">Cancel</button>
          </div>
        </div>
      )}

      {(action === 'create' || action === 'rename') && (
        <form onSubmit={submit} className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <input
            aria-label="Profile label"
            value={label}
            onChange={event => setLabel(event.target.value)}
            maxLength={80}
            required
            autoFocus
            className="min-w-0 flex-1 rounded-lg border border-border bg-bg px-3 py-2 text-sm text-ink"
          />
          <button type="submit" disabled={busy || !label.trim()} className="rounded-lg bg-brand-600 px-3 py-2 text-sm text-white disabled:opacity-50">{action === 'create' ? 'Create profile' : 'Save profile'}</button>
          <button type="button" disabled={busy} onClick={() => begin(null)} className="text-sm text-muted hover:text-ink">Cancel</button>
        </form>
      )}
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  );
}
