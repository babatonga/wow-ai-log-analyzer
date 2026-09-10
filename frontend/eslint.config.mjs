// ESLint flat config. eslint-config-next ships flat exports since 16
// (`next lint` itself was removed in Next 16 — the `lint` script calls
// the eslint CLI directly).
//
// ESLint is pinned to the 9.x maintenance line for now: eslint 10
// requires scope managers to implement addGlobals(), which
// typescript-eslint (8.69, newest at time of writing) doesn't ship yet
// — `eslint .` crashes with "scopeManager.addGlobals is not a
// function". Revisit when typescript-eslint declares eslint 10 support.
import coreWebVitals from "eslint-config-next/core-web-vitals";

const base = Array.isArray(coreWebVitals) ? coreWebVitals : [coreWebVitals];

const config = [
  ...base,
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts"],
  },
  {
    rules: {
      // react-hooks v7 (via eslint-config-next 16) promotes this to
      // error. The codebase deliberately syncs fetched server state
      // into local form state via useEffect in a handful of panels
      // (admin, simulate, Header, …) — rewriting those to
      // derive-during-render is a behavioural refactor that doesn't
      // belong in a dependency-update PR. Downgraded to warn; tracked
      // as a follow-up.
      "react-hooks/set-state-in-effect": "warn",
    },
  },
];

export default config;
