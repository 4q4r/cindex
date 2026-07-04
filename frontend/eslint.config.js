import js from "@eslint/js";
import tseslint from "typescript-eslint";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";

// Strictest production ESLint 9 flat config for the Vite + React 19 + TS frontend.
// Researched via Exa MCP (GitHub Docs, codewithseb 2026, javascript-news 2026,
// pkglog 2026): type-checked typescript-eslint (strictTypeChecked +
// stylisticTypeChecked) is the strictest available preset; combined with the
// React, react-hooks, jsx-a11y (strict) and react-refresh plugin recommended
// configs. Run with: bun run lint (errors) / bun run lint:check (CI, --max-warnings 0).

export default tseslint.config(
  {
    ignores: [
      "dist/**",
      ".vite-cache/**",
      "node_modules/**",
      "eslint.config.js",
      "*.config.ts",
      "src/vite-env.d.ts",
    ],
  },

  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.strictTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
      react.configs.flat.recommended,
      react.configs.flat["jsx-runtime"],
      reactHooks.configs["flat/recommended"],
      jsxA11y.flatConfigs.strict,
      reactRefresh.configs.recommended,
    ],
    languageOptions: {
      globals: {
        ...globals.browser,
      },
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    settings: {
      react: {
        version: "detect",
      },
    },
    rules: {
      // React 19 + JSX runtime: no need for explicit React import or prop-types.
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",
      // Keep unused-var checking under typescript-eslint (covers type positions).
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },

  {
    // Config files are Node-context, not browser. Lint them without the
    // type-checked React/browser presets.
    files: ["vite.config.ts"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommendedTypeChecked,
    ],
    languageOptions: {
      globals: {
        ...globals.node,
      },
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
);
