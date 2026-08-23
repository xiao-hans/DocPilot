/** @type {import('tailwindcss').Config} */
// 设计令牌映射：颜色/字体/圆角全部指向 src/assets/tokens.css 的 CSS 变量，
// 组件内不出现任何裸色值。类名约定：bg-canvas / bg-surface / border-edge /
// text-primary / bg-brand / rounded-card / rounded-control / duration-fast|act。
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        canvas: 'var(--bg)', // 页面底色 #0B0B0F
        surface: 'var(--surface)', // 卡片 #141419
        'surface-2': 'var(--surface-2)', // 表头/次级块 #1C1C22
        edge: 'var(--border)', // 边框 #2A2A33
        primary: 'var(--text-primary)', // 主文字 #F3F3F0
        secondary: 'var(--text-secondary)', // 次级文字 #A0A0A8
        muted: 'var(--text-muted)', // 弱化文字 #6E6E78
        brand: {
          DEFAULT: 'var(--brand)',
          hover: 'var(--brand-hover)',
        },
        'on-brand': 'var(--on-brand)',
        success: 'var(--success)',
        danger: 'var(--danger)',
      },
      fontFamily: {
        display: ['var(--font-display)'], // Inter（标题）
        body: ['var(--font-body)'], // Open Sans（正文）
        mono: ['var(--font-mono)'], // Inconsolata（数值/ID）
      },
      borderRadius: {
        card: 'var(--radius-card)', // 卡片 12
        control: 'var(--radius-control)', // 控件 8
      },
      transitionDuration: {
        fast: 'var(--duration-fast)', // 150ms
        act: 'var(--duration-act)', // 400ms（切幕）
      },
    },
  },
  plugins: [],
}