// The config DiffLens lints reviewed code with. It is deliberately not the
// config of the repository under review: a project's own rules are its style
// preferences, and this tool reports defects, not preferences.
//
// Three constraints shape it:
//
// 1. No type-aware rules. Those need the target's tsconfig and its installed
//    node_modules, neither of which exists in a workspace built from the
//    GitHub contents API. Asking for them would make the analyzer fail on
//    every real pull request.
// 2. No stylistic rules. Semicolons and quote marks are not findings, and a
//    reviewer that opens with fifty of them does not get read.
// 3. Globals for both browser and node, because the analyzer cannot know
//    which one a changed file runs in, and guessing wrong invents no-undef
//    findings that are purely an artefact of the guess.

import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default [
  {
    ignores: ["**/node_modules/**", "**/dist/**", "**/build/**", "**/*.min.js"],
  },
  {
    files: ["**/*.{js,mjs,cjs,jsx,ts,mts,cts,tsx}"],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
        // No "project": type-aware linting is off on purpose, see above
      },
      globals: { ...globals.browser, ...globals.node, ...globals.es2025 },
    },
    plugins: { "@typescript-eslint": tseslint.plugin },
    rules: {
      ...js.configs.recommended.rules,

      // Correctness, high confidence, worth a reviewer's attention
      "no-undef": "error",
      "no-unreachable": "error",
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-dupe-class-members": "error",
      "no-duplicate-case": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-constant-condition": ["error", { checkLoops: false }],
      "no-unsafe-negation": "error",
      "no-unsafe-optional-chaining": "error",
      "no-sparse-arrays": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
      // Off: the rule reports any assignment to a member of an object that
      // was live across an await, which is what almost every async test
      // helper and request handler does legitimately. Measured on a real
      // repository it produced five findings and zero defects.
      "require-atomic-updates": "off",
      "no-await-in-loop": "off", // often correct, and not a defect
      "no-fallthrough": "error",
      "no-cond-assign": ["error", "always"],
      "no-compare-neg-zero": "error",
      "no-async-promise-executor": "error",
      // Off: its dominant real-world trigger is the standard sleep idiom,
      // new Promise((r) => setTimeout(r, ms)), where the returned timer id
      // is harmless. Same repository: twenty-five findings, zero defects.
      "no-promise-executor-return": "off",
      "no-unmodified-loop-condition": "error",

      // Security
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-script-url": "error",

      // Maintainability, reported quietly
      "no-unused-vars": ["error", { args: "none", ignoreRestSiblings: true }],
      "no-debugger": "error",
      "no-console": "off", // a console.log is not a defect

      // Off: stylistic, or noisy without type information
      "no-empty": "off",
      "no-prototype-builtins": "off",
      "no-useless-escape": "off",
      "no-control-regex": "off",
    },
  },
  {
    // TypeScript's own compiler already rejects undefined identifiers, and
    // no-undef cannot see type-only declarations, so it invents findings here.
    // The others are base rules that do not understand TypeScript syntax:
    // no-redeclare fires on every overload signature and on declaration
    // merging, no-dupe-class-members on overloaded methods, and the base
    // no-unused-vars does not know that a type-only import is a use. Each is
    // handed to the typescript-eslint version, which does understand.
    files: ["**/*.{ts,mts,cts,tsx}"],
    rules: {
      "no-undef": "off",
      "no-redeclare": "off",
      "no-dupe-class-members": "off",
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { args: "none", ignoreRestSiblings: true },
      ],
      "@typescript-eslint/no-dupe-class-members": "error",
    },
  },
  {
    // A test file's globals come from a runner nobody installed in this
    // workspace. Without them every describe/it/expect is a high-severity
    // no-undef, which buries the real findings in any repository that has
    // tests, meaning every repository worth reviewing.
    files: [
      "**/*.{test,spec}.{js,mjs,cjs,jsx,ts,mts,cts,tsx}",
      "**/__tests__/**",
      "**/__mocks__/**",
      "**/test/**",
      "**/tests/**",
    ],
    languageOptions: {
      globals: { ...globals.jest, ...globals.mocha, ...globals.vitest },
    },
  },
];
