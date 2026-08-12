import { useState } from 'react';
import { Drawer } from '@mantine/core';
import { Activity, MoonStar, RefreshCw } from 'lucide-react';
import { API_BASE, useEngineHealth } from '../hooks/useEngineHealth';
import './SystemPulse.css';

export function SystemPulse() {
  const [opened, setOpened] = useState(false);
  const { pulse, check } = useEngineHealth();

  const awake = pulse.kind === 'awake';

  return (
    <>
      <button
        type="button"
        className={`pulse-tab ${awake ? 'pulse-tab-awake' : ''}`}
        onClick={() => setOpened(true)}
        aria-label="Prediction engine status"
      >
        <span className={`pulse-beacon ${awake ? 'pulse-beacon-awake' : ''}`} />
        <span className="pulse-tab-text">Engine</span>
      </button>

      <Drawer
        opened={opened}
        onClose={() => setOpened(false)}
        position="right"
        size={330}
        title="Prediction engine"
        overlayProps={{ blur: 4, color: '#0d1028', backgroundOpacity: 0.5 }}
        classNames={{ content: 'pulse-drawer', header: 'pulse-drawer-header', title: 'pulse-drawer-title' }}
      >
        <div className="pulse-card">
          {pulse.kind === 'checking' && (
            <>
              <RefreshCw className="pulse-icon pulse-spin" size={30} />
              <h3>Checking…</h3>
              <p>Reaching out to the engine.</p>
            </>
          )}
          {pulse.kind === 'awake' && (
            <>
              <Activity className="pulse-icon pulse-icon-awake" size={30} />
              <h3>Awake and listening</h3>
              <p>
                Responded in <span className="mono">{pulse.latencyMs} ms</span>. Forecasts on this
                page come from the live model.
              </p>
            </>
          )}
          {pulse.kind === 'asleep' && (
            <>
              <MoonStar className="pulse-icon" size={30} />
              <h3>Resting for now</h3>
              <p>
                The engine is not reachable, so the flights shown are a stable preview. Everything
                else on the page still works.
              </p>
            </>
          )}
        </div>

        <dl className="pulse-facts">
          <div>
            <dt>Endpoint</dt>
            <dd className="mono">{API_BASE}/health</dd>
          </div>
          <div>
            <dt>Checked every</dt>
            <dd>30 seconds</dd>
          </div>
          {pulse.kind !== 'checking' && (
            <div>
              <dt>Last check</dt>
              <dd className="mono">
                {pulse.checkedAt.toLocaleTimeString('en-US', { hour12: false })}
              </dd>
            </div>
          )}
        </dl>

        <button type="button" className="pulse-refresh" onClick={check}>
          <RefreshCw size={14} /> Check again now
        </button>
      </Drawer>
    </>
  );
}
