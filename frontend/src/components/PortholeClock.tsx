import { useEffect, useMemo, useState } from 'react';
import { Popover, Select } from '@mantine/core';
import './PortholeClock.css';

const FEATURED_ZONES: Array<{ value: string; label: string }> = [
  { value: 'Asia/Singapore', label: 'Singapore (SGT)' },
  { value: 'America/New_York', label: 'New York (ET)' },
  { value: 'America/Chicago', label: 'Chicago (CT)' },
  { value: 'America/Los_Angeles', label: 'Los Angeles (PT)' },
  { value: 'Europe/London', label: 'London (BST/GMT)' },
  { value: 'Europe/Paris', label: 'Paris (CET)' },
  { value: 'Asia/Dubai', label: 'Dubai (GST)' },
  { value: 'Asia/Tokyo', label: 'Tokyo (JST)' },
  { value: 'Asia/Seoul', label: 'Seoul (KST)' },
  { value: 'Asia/Hong_Kong', label: 'Hong Kong (HKT)' },
  { value: 'Australia/Sydney', label: 'Sydney (AET)' },
  { value: 'Asia/Kolkata', label: 'India (IST)' },
];

function zoneOptions(): Array<{ group: string; items: Array<{ value: string; label: string }> }> {
  const featuredValues = new Set(FEATURED_ZONES.map((z) => z.value));
  let all: string[] = [];
  try {
    all = Intl.supportedValuesOf('timeZone');
  } catch {
    all = [];
  }
  const rest = all
    .filter((z) => !featuredValues.has(z) && z.includes('/'))
    .map((z) => ({ value: z, label: z.replace(/_/g, ' ') }));
  return [
    { group: 'Frequent', items: FEATURED_ZONES },
    { group: 'Everywhere else', items: rest },
  ];
}

function timeIn(zone: string): { h: number; m: number; s: number } {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: zone,
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric',
    hour12: false,
  }).formatToParts(new Date());
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  return { h: get('hour') % 24, m: get('minute'), s: get('second') };
}

function zoneAbbr(zone: string): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: zone,
    timeZoneName: 'short',
  }).formatToParts(new Date());
  return parts.find((p) => p.type === 'timeZoneName')?.value ?? zone;
}

/**
 * The page's signature: an analog clock set inside an airplane-window
 * porthole. Clicking it opens the timezone switcher.
 */
export function PortholeClock() {
  const [zone, setZone] = useState('Asia/Singapore');
  const [now, setNow] = useState(() => timeIn('Asia/Singapore'));
  const [opened, setOpened] = useState(false);

  useEffect(() => {
    setNow(timeIn(zone));
    const t = setInterval(() => setNow(timeIn(zone)), 1000);
    return () => clearInterval(t);
  }, [zone]);

  const options = useMemo(zoneOptions, []);

  const hourAngle = ((now.h % 12) + now.m / 60) * 30;
  const minuteAngle = (now.m + now.s / 60) * 6;
  const secondAngle = now.s * 6;
  const city = zone.split('/').pop()?.replace(/_/g, ' ') ?? zone;

  const digital = `${String(now.h).padStart(2, '0')}:${String(now.m).padStart(2, '0')}`;

  return (
    <Popover
      opened={opened}
      onChange={setOpened}
      position="bottom-end"
      offset={10}
      trapFocus
      classNames={{ dropdown: 'porthole-dropdown' }}
    >
      <Popover.Target>
        <button
          type="button"
          className="porthole"
          onClick={() => setOpened((o) => !o)}
          aria-label={`Clock showing ${digital} in ${city}. Click to change region.`}
          title="Change region"
        >
          <div className="porthole-window">
            <svg viewBox="0 0 100 100" className="porthole-face" aria-hidden="true">
              {Array.from({ length: 12 }, (_, i) => {
                const a = (i * 30 * Math.PI) / 180;
                const isQuarter = i % 3 === 0;
                const r1 = isQuarter ? 38 : 41;
                return (
                  <line
                    key={i}
                    x1={50 + r1 * Math.sin(a)}
                    y1={50 - r1 * Math.cos(a)}
                    x2={50 + 44 * Math.sin(a)}
                    y2={50 - 44 * Math.cos(a)}
                    stroke={isQuarter ? 'rgba(244,240,232,0.9)' : 'rgba(244,240,232,0.4)'}
                    strokeWidth={isQuarter ? 2.4 : 1.2}
                    strokeLinecap="round"
                  />
                );
              })}
              <line
                x1="50"
                y1="50"
                x2={50 + 22 * Math.sin((hourAngle * Math.PI) / 180)}
                y2={50 - 22 * Math.cos((hourAngle * Math.PI) / 180)}
                stroke="#f4f0e8"
                strokeWidth="3.4"
                strokeLinecap="round"
              />
              <line
                x1="50"
                y1="50"
                x2={50 + 33 * Math.sin((minuteAngle * Math.PI) / 180)}
                y2={50 - 33 * Math.cos((minuteAngle * Math.PI) / 180)}
                stroke="#e8dfd0"
                strokeWidth="2.2"
                strokeLinecap="round"
              />
              <line
                x1={50 - 8 * Math.sin((secondAngle * Math.PI) / 180)}
                y1={50 + 8 * Math.cos((secondAngle * Math.PI) / 180)}
                x2={50 + 38 * Math.sin((secondAngle * Math.PI) / 180)}
                y2={50 - 38 * Math.cos((secondAngle * Math.PI) / 180)}
                stroke="var(--calm-coral)"
                strokeWidth="1.1"
                strokeLinecap="round"
                className="porthole-second-hand"
              />
              <circle cx="50" cy="50" r="2.6" fill="#f4f0e8" />
            </svg>
          </div>
          <div className="porthole-label">
            <span className="porthole-city">{city}</span>
            <span className="porthole-zone mono">
              {digital} {zoneAbbr(zone)}
            </span>
          </div>
        </button>
      </Popover.Target>
      <Popover.Dropdown>
        <p className="porthole-hint">Show times somewhere else</p>
        <Select
          data={options}
          value={zone}
          onChange={(v) => {
            if (v) {
              setZone(v);
              setOpened(false);
            }
          }}
          searchable
          placeholder="Search a city or region"
          nothingFoundMessage="No matching region"
          maxDropdownHeight={240}
          comboboxProps={{ withinPortal: false }}
        />
      </Popover.Dropdown>
    </Popover>
  );
}
