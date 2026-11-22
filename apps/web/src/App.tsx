import React, { useEffect, useState } from 'react';
import { getReadyz, ReadyzResponse } from './api/client';

export const App: React.FC = () => {
  const [data, setData] = useState<ReadyzResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let isMounted = true;

    async function loadStatus(): Promise<void> {
      try {
        setLoading(true);
        setError(null);
        const res = await getReadyz();
        if (isMounted) {
          setData(res);
        }
      } catch (err: unknown) {
        if (isMounted) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadStatus();
    return () => {
      isMounted = false;
    };
  }, []);

  return (
    <main
      style={{
        fontFamily: 'sans-serif',
        padding: '2rem',
        maxWidth: '600px',
        margin: '0 auto',
      }}
    >
      <h1>OpsPilot</h1>
      <p>Incident Investigation &amp; Recovery Platform</p>

      <section
        style={{
          marginTop: '2rem',
          padding: '1rem',
          border: '1px solid #ccc',
          borderRadius: '8px',
        }}
      >
        <h2>System Status</h2>
        {loading && <p>Checking status...</p>}
        {error && (
          <p role="alert" style={{ color: 'red' }}>
            Error: {error}
          </p>
        )}
        {data && (
          <div>
            <p>
              <strong>Overall:</strong>{' '}
              <span
                data-testid="overall-status"
                style={{ color: data.status === 'ok' ? 'green' : 'crimson' }}
              >
                {data.status}
              </span>
            </p>
            <ul>
              <li>
                postgres:{' '}
                <span
                  data-testid="postgres-status"
                  style={{
                    color:
                      data.components.postgres === 'ok' ? 'green' : 'crimson',
                  }}
                >
                  {data.components.postgres}
                </span>
              </li>
              <li>
                redis:{' '}
                <span
                  data-testid="redis-status"
                  style={{
                    color:
                      data.components.redis === 'ok' ? 'green' : 'crimson',
                  }}
                >
                  {data.components.redis}
                </span>
              </li>
            </ul>
          </div>
        )}
      </section>
    </main>
  );
};

export default App;
