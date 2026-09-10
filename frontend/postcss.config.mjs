// Tailwind 4: the PostCSS plugin moved into @tailwindcss/postcss and
// includes vendor prefixing (Lightning CSS) — no separate autoprefixer.
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
