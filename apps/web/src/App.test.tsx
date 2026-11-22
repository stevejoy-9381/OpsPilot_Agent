import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import * as client from './api/client';

describe('OpsPilot App', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders "ok" status when all dependencies are healthy', async () => {
    vi.spyOn(client, 'getReadyz').mockResolvedValueOnce({
      status: 'ok',
      components: {
        postgres: 'ok',
        redis: 'ok',
      },
    });

    render(<App />);

    expect(await screen.findByText('OpsPilot')).toBeInTheDocument();
    expect(await screen.findByTestId('overall-status')).toHaveTextContent('ok');
    expect(screen.getByTestId('postgres-status')).toHaveTextContent('ok');
    expect(screen.getByTestId('redis-status')).toHaveTextContent('ok');
  });

  it('renders "degraded" status when a dependency is degraded', async () => {
    vi.spyOn(client, 'getReadyz').mockResolvedValueOnce({
      status: 'degraded',
      components: {
        postgres: 'degraded',
        redis: 'ok',
      },
    });

    render(<App />);

    expect(await screen.findByText('OpsPilot')).toBeInTheDocument();
    expect(await screen.findByTestId('overall-status')).toHaveTextContent(
      'degraded'
    );
    expect(screen.getByTestId('postgres-status')).toHaveTextContent('degraded');
    expect(screen.getByTestId('redis-status')).toHaveTextContent('ok');
  });
});
