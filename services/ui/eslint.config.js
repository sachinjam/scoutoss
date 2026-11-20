import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // A concise arrow returns its expression, and React calls a returned value as the effect's
      // cleanup. Element.scrollIntoView() now returns a Promise in Chrome, which crashed the UI.
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name=/^use(Layout|Insertion)?Effect$/] > ArrowFunctionExpression[expression=true]",
          message: "Effect callbacks need a block body: a returned value is treated as the cleanup function.",
        },
      ],
    },
  },
);
