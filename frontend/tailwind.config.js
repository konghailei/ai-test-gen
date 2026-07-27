/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Apple 系统蓝（主色）
        primary: {
          50: '#e8f2ff',
          100: '#cfe5ff',
          200: '#9fcbff',
          300: '#66abff',
          400: '#2e8dff',
          500: '#007aff',
          600: '#0064d6',
          700: '#004fad',
          800: '#003b82',
          900: '#00275a',
          950: '#001a3d',
        },
        // Apple 靛蓝（次强调色）
        accent: {
          50: '#f0f0ff',
          100: '#e0e0ff',
          200: '#c7c7ff',
          300: '#a3a3ff',
          400: '#7a7aff',
          500: '#5856d6',
          600: '#4a48c4',
          700: '#3d3ba0',
          800: '#2e2c7a',
          900: '#1f1d52',
        },
        // Apple 系统绿
        success: {
          50: '#e9f9ee',
          100: '#c8f0d2',
          200: '#9ae3ad',
          300: '#5fd584',
          400: '#34c759',
          500: '#30d158',
          600: '#28a745',
        },
        // Apple 系统橙
        warning: {
          50: '#fff4e6',
          100: '#ffe0b3',
          200: '#ffcc80',
          300: '#ffb74d',
          400: '#ff9500',
          500: '#f08c00',
          600: '#cc7000',
        },
        // Apple 系统红
        danger: {
          50: '#ffecea',
          100: '#ffcfcf',
          200: '#ffa3a3',
          300: '#ff6b6b',
          400: '#ff453a',
          500: '#ff3b30',
          600: '#d70015',
        },
        // Apple 中性灰阶（background / text / icon 共用同一套灰）
        apple: {
          50: '#ffffff',
          100: '#f7f7fa',
          200: '#f2f2f7', // muted
          300: '#e5e5ea',
          400: '#d1d1d6',
          500: '#aeaeb2',
          600: '#8e8e93', // muted-foreground
          700: '#6e6e73',
          800: '#3a3a3c',
          900: '#1c1c1e',
          950: '#000000',
          // 语义别名
          'background': '#ffffff',
          'foreground': '#1d1d1f',
          'card': '#ffffff',
          'muted': '#f2f2f7',
          'muted-foreground': '#8e8e93',
          'border': '#e5e5ea',
          'input': '#d1d1d6',
        },
      },
      fontFamily: {
        sans: ['DM Sans', 'PingFang SC', 'Microsoft YaHei', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      borderRadius: {
        'apple-sm': '0.6rem',
        'apple-md': '0.72rem',
        'apple': '1.2rem',
        'apple-lg': '1.8rem',
      },
      boxShadow: {
        'apple-2xs': '0 1px 2px -1px rgba(0, 0, 0, 0.04)',
        'apple-xs': '0 1px 2px 0 rgba(0, 0, 0, 0.04)',
        'apple-sm': '0 1px 2px 0 rgba(0, 0, 0, 0.05), 0 1px 3px -1px rgba(0, 0, 0, 0.05)',
        'apple': '0 2px 4px -1px rgba(0, 0, 0, 0.06), 0 1px 2px -1px rgba(0, 0, 0, 0.05)',
        'apple-md': '0 4px 8px -2px rgba(0, 0, 0, 0.06), 0 2px 4px -2px rgba(0, 0, 0, 0.05)',
        'apple-lg': '0 8px 24px -8px rgba(0, 0, 0, 0.08), 0 4px 8px -4px rgba(0, 0, 0, 0.05)',
        'apple-xl': '0 16px 40px -10px rgba(0, 0, 0, 0.10), 0 8px 16px -8px rgba(0, 0, 0, 0.06)',
        'apple-2xl': '0 24px 64px -12px rgba(0, 0, 0, 0.12)',
      },
      animation: {
        'gradient': 'gradient 8s linear infinite',
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'slide-up': 'slideUp 0.3s ease-out',
        'fade-in': 'fadeIn 0.5s ease-out',
        'scale-in': 'scaleIn 0.18s ease-out',
      },
      keyframes: {
        gradient: {
          '0%, 100%': { backgroundPosition: '0% 50%' },
          '50%': { backgroundPosition: '100% 50%' },
        },
        slideUp: {
          '0%': { transform: 'translateY(10px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        scaleIn: {
          '0%': { transform: 'scale(0.96)', opacity: '0' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}
