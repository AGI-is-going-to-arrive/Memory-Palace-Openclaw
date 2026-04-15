import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

import i18n from '../../i18n';
import MemoryBrowser from './MemoryBrowser';
import * as api from '../../lib/api';

vi.mock('../../lib/api', () => ({
  createMemoryNode: vi.fn(),
  deleteMemoryNode: vi.fn(),
  getMemoryNode: vi.fn(),
  updateMemoryNode: vi.fn(),
}));

const createDeferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const ROOT_PAYLOAD = {
  node: null,
  children: [],
  breadcrumbs: [{ path: '', label: 'root' }],
};

const makeNodePayload = (path, content) => ({
  node: {
    path,
    domain: 'core',
    uri: `core://${path}`,
    name: path,
    content,
    priority: 0,
    disclosure: '',
    gist_text: null,
    gist_method: null,
    gist_quality: null,
    source_hash: null,
  },
  children: [],
  breadcrumbs: [
    { path: '', label: 'root' },
    { path, label: path },
  ],
});

const renderMemoryBrowser = (entry) =>
  render(
    <MemoryRouter
      initialEntries={[entry]}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <Routes>
        <Route path="/memory" element={<MemoryBrowser />} />
      </Routes>
    </MemoryRouter>
  );

function RaceHarness() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate('/memory?domain=core&path=path-b')}>
        Go path-b
      </button>
      <MemoryBrowser />
    </>
  );
}

describe('MemoryBrowser', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage('en');
    api.getMemoryNode.mockResolvedValue(ROOT_PAYLOAD);
    api.createMemoryNode.mockResolvedValue({ success: true, created: true, path: 'created/path', domain: 'core', uri: 'core://created/path' });
    api.updateMemoryNode.mockResolvedValue({ success: true, updated: true });
    api.deleteMemoryNode.mockResolvedValue({ success: true });
  });

  it('does not navigate and shows guard feedback when create returns created=false', async () => {
    const user = userEvent.setup();
    api.createMemoryNode.mockResolvedValue({
      success: true,
      created: false,
      guard_action: 'UPDATE',
      guard_reason: 'duplicate_memory_candidate',
      guard_target_uri: 'core://agents/alpha/captured/workflow/existing-note',
      guard_user_reason:
        'This looks close to an existing memory, so automatic storage paused before creating a duplicate.',
      guard_recovery_hint:
        'Review the suggested memory first, or choose Store anyway if you still want this saved here.',
      force_write_available: true,
      message: 'Skipped: write_guard blocked create_node (action=NOOP, method=hybrid).',
    });

    renderMemoryBrowser('/memory?domain=core');

    const storeButton = await screen.findByRole('button', { name: /Store Memory/i });
    await user.type(screen.getByPlaceholderText(/Paste LLM \/ agent dialogue/i), 'remember this workflow');
    await user.click(storeButton);

    await screen.findByText(/Automatic storage paused for review/i);
    expect(screen.getByRole('button', { name: /Store anyway/i })).toBeInTheDocument();
    expect(
      screen.getByText(/core:\/\/agents\/alpha\/captured\/workflow\/existing-note/i)
    ).toBeInTheDocument();
    expect(api.createMemoryNode).toHaveBeenCalledTimes(1);
    expect(api.getMemoryNode).toHaveBeenCalledTimes(1);
    expect(
      api.getMemoryNode.mock.calls.some(([params]) => params?.domain === 'undefined')
    ).toBe(false);
  });

  it('retries create with force_write after user confirmation', async () => {
    const user = userEvent.setup();
    api.createMemoryNode
      .mockResolvedValueOnce({
        success: true,
        created: false,
        guard_action: 'NOOP',
        guard_reason: 'duplicate_memory_candidate',
        guard_user_reason:
          'This may duplicate an existing memory, so automatic storage paused first.',
        guard_recovery_hint:
          'Review the suggested memory first, or choose Store anyway if you still want this saved here.',
        force_write_available: true,
      })
      .mockResolvedValueOnce({
        success: true,
        created: true,
        guard_overridden: true,
        path: 'created/path',
        domain: 'core',
        uri: 'core://created/path',
      });

    renderMemoryBrowser('/memory?domain=core');

    await user.type(
      await screen.findByPlaceholderText(/Paste LLM \/ agent dialogue/i),
      'remember this workflow'
    );
    await user.click(screen.getByRole('button', { name: /Store Memory/i }));
    await user.click(await screen.findByRole('button', { name: /Store anyway/i }));

    await screen.findByText(/after your confirmation/i);
    expect(api.createMemoryNode).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        force_write: true,
        content: 'remember this workflow',
      })
    );
  });

  it('keeps the conversation vault empty until the user types real content', async () => {
    const user = userEvent.setup();

    renderMemoryBrowser('/memory?domain=core');

    const composer = await screen.findByPlaceholderText(/Paste LLM \/ agent dialogue/i);
    expect(composer).toHaveValue('');

    await user.click(screen.getByRole('button', { name: /Store Memory/i }));

    expect(api.createMemoryNode).not.toHaveBeenCalled();
    expect(await screen.findByText(/Conversation content cannot be empty/i)).toBeInTheDocument();
  });

  it('shows write_guard skip feedback when update returns updated=false', async () => {
    const user = userEvent.setup();
    api.getMemoryNode.mockResolvedValueOnce(makeNodePayload('path-a', 'old content'));
    api.updateMemoryNode.mockResolvedValue({
      success: true,
      updated: false,
      guard_action: 'NOOP',
      guard_reason: 'write_guard_unavailable: timeout',
      force_write_available: true,
      message: 'Skipped: write_guard blocked update_node (action=NOOP, method=hybrid).',
    });

    renderMemoryBrowser('/memory?domain=core&path=path-a');

    const editButton = await screen.findByRole('button', { name: /Edit/i });
    await user.click(editButton);

    const textarea = await screen.findByDisplayValue('old content');
    await user.clear(textarea);
    await user.type(textarea, 'old content changed');
    await user.click(screen.getByRole('button', { name: /Save/i }));

    await screen.findByText(/Automatic update paused for review/i);
    expect(screen.getByRole('button', { name: /Store anyway/i })).toBeInTheDocument();
    expect(screen.queryByText('Long-term memory updated.')).not.toBeInTheDocument();
    expect(api.updateMemoryNode).toHaveBeenCalledTimes(1);
    expect(api.getMemoryNode).toHaveBeenCalledTimes(1);
  });

  it('retries update with force_write after user confirmation', async () => {
    const user = userEvent.setup();
    api.getMemoryNode.mockResolvedValue(makeNodePayload('path-a', 'old content'));
    api.updateMemoryNode
      .mockResolvedValueOnce({
        success: true,
        updated: false,
        guard_action: 'UPDATE',
        guard_reason: 'duplicate_memory_candidate',
        guard_target_uri: 'core://agent/existing',
        guard_user_reason:
          'This looks close to an existing memory, so automatic storage paused before creating a duplicate.',
        force_write_available: true,
      })
      .mockResolvedValueOnce({
        success: true,
        updated: true,
        guard_overridden: true,
      });

    renderMemoryBrowser('/memory?domain=core&path=path-a');

    await user.click(await screen.findByRole('button', { name: /Edit/i }));
    const textarea = await screen.findByDisplayValue('old content');
    await user.clear(textarea);
    await user.type(textarea, 'old content changed');
    await user.click(screen.getByRole('button', { name: /Save/i }));
    await user.click(await screen.findByRole('button', { name: /Store anyway/i }));

    await screen.findByText(/updated after your confirmation/i);
    expect(api.updateMemoryNode).toHaveBeenNthCalledWith(
      2,
      'path-a',
      'core',
      expect.objectContaining({
        content: 'old content changed',
        force_write: true,
      })
    );
  });

  it('ignores stale node responses when path switches quickly', async () => {
    const user = userEvent.setup();
    const deferredA = createDeferred();
    const deferredB = createDeferred();

    api.getMemoryNode.mockImplementation(({ path }) => {
      if (path === 'path-a') return deferredA.promise;
      if (path === 'path-b') return deferredB.promise;
      return Promise.resolve(ROOT_PAYLOAD);
    });

    render(
      <MemoryRouter
        initialEntries={['/memory?domain=core&path=path-a']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/memory" element={<RaceHarness />} />
        </Routes>
      </MemoryRouter>
    );

    await user.click(screen.getByRole('button', { name: /Go path-b/i }));

    deferredB.resolve(makeNodePayload('path-b', 'fresh content B'));
    await screen.findByText('fresh content B');

    deferredA.resolve(makeNodePayload('path-a', 'stale content A'));
    await waitFor(() => {
      expect(screen.queryByText('stale content A')).not.toBeInTheDocument();
    });
    expect(screen.getByText('fresh content B')).toBeInTheDocument();
  });
});
