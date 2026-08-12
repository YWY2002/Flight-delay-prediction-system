import { createTheme } from '@mantine/core';

/** Mantine theme tuned to the Halcyon twilight palette. */
export const theme = createTheme({
  fontFamily: "'Nunito Sans Variable', system-ui, sans-serif",
  fontFamilyMonospace: "'IBM Plex Mono', ui-monospace, monospace",
  headings: {
    fontFamily: "'Bricolage Grotesque Variable', system-ui, sans-serif",
  },
  primaryColor: 'sand',
  colors: {
    sand: [
      '#fdf6ec',
      '#f8e9d4',
      '#f2c98f',
      '#ecb976',
      '#e6a95e',
      '#d99873',
      '#c2825e',
      '#a56b4b',
      '#875439',
      '#6a3f29',
    ],
  },
  defaultRadius: 'md',
  components: {
    ScrollArea: {
      defaultProps: { scrollbarSize: 8 },
      styles: {
        thumb: { background: 'rgba(200, 195, 230, 0.28)' },
        scrollbar: { background: 'transparent' },
      },
    },
    SegmentedControl: {
      styles: {
        root: {
          background: 'rgba(20, 24, 52, 0.55)',
          border: '1px solid rgba(255, 250, 240, 0.09)',
        },
        indicator: { background: 'rgba(242, 201, 143, 0.22)' },
        label: { color: 'var(--ink-dim)' },
      },
    },
    TextInput: {
      styles: {
        input: {
          background: 'rgba(20, 24, 52, 0.55)',
          border: '1px solid rgba(255, 250, 240, 0.12)',
          color: 'var(--ink)',
        },
        label: { color: 'var(--ink-dim)', marginBottom: 4 },
      },
    },
    PasswordInput: {
      styles: {
        input: {
          background: 'rgba(20, 24, 52, 0.55)',
          border: '1px solid rgba(255, 250, 240, 0.12)',
        },
        innerInput: { color: 'var(--ink)' },
        label: { color: 'var(--ink-dim)', marginBottom: 4 },
      },
    },
    Tooltip: {
      styles: {
        tooltip: {
          background: 'rgba(30, 34, 68, 0.97)',
          border: '1px solid rgba(255, 250, 240, 0.14)',
          color: 'var(--ink)',
          fontSize: '0.72rem',
        },
      },
    },
    MultiSelect: {
      styles: {
        input: {
          background: 'rgba(20, 24, 52, 0.55)',
          border: '1px solid rgba(255, 250, 240, 0.12)',
          color: 'var(--ink)',
        },
        pill: { background: 'rgba(242, 201, 143, 0.2)', color: 'var(--ink)' },
        dropdown: {
          background: 'rgba(30, 34, 68, 0.97)',
          border: '1px solid rgba(255, 250, 240, 0.14)',
          color: 'var(--ink)',
        },
        option: { color: 'var(--ink)' },
      },
    },
    Select: {
      styles: {
        input: {
          background: 'rgba(20, 24, 52, 0.55)',
          border: '1px solid rgba(255, 250, 240, 0.12)',
          color: 'var(--ink)',
        },
        dropdown: {
          background: 'rgba(30, 34, 68, 0.97)',
          border: '1px solid rgba(255, 250, 240, 0.14)',
          color: 'var(--ink)',
        },
        option: { color: 'var(--ink)' },
      },
    },
    Popover: {
      styles: {
        dropdown: {
          background: 'rgba(30, 34, 68, 0.97)',
          border: '1px solid rgba(255, 250, 240, 0.14)',
          color: 'var(--ink)',
        },
      },
    },
  },
});
