// ESLint flat config.
//
// Replaces `.eslintrc.json` + `next lint`. `next lint` is deprecated (removed
// in Next 16) and drives the ESLint Node API with options newer ESLint has
// deleted, so lint now runs through the ESLint CLI (`eslint .`).
// eslint-config-next 16 exports flat config directly, which is what this
// consumes.
//
// Deliberately avoids the `eslint/config` helpers (`defineConfig`,
// `globalIgnores`): those live in a module that only exists in newer ESLint,
// and this must keep working on the pinned 9.x. A plain array plus an
// `ignores` entry is the portable spelling.
//
// Rule set is unchanged from the old `{ "extends": "next/core-web-vitals" }`
// — this is a migration, not a policy change. `eslint-config-next/typescript`
// is NOT added: it layers typescript-eslint's recommended rules on top and
// would flag pre-existing code the previous config never checked.
import nextVitals from "eslint-config-next/core-web-vitals";

// Named rather than an anonymous array literal — the config lints itself,
// and `import/no-anonymous-default-export` warns otherwise.
const eslintConfig = [
  ...nextVitals,
  // Re-declare eslint-config-next's default ignores: spreading its flat
  // config does not carry them over.
  { ignores: [".next/**", "out/**", "build/**", "next-env.d.ts"] },
];

export default eslintConfig;
