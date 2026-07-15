import { describe, expect, it } from "vitest";

import { TOOLCHAIN_BASELINE } from "./toolchain";

describe("frontend toolchain baseline", () => {
  it("keeps the approved runtime versions visible", () => {
    expect(TOOLCHAIN_BASELINE).toEqual({
      react: "19.2",
      node: "24.18.0",
      packageManager: "npm@11.16.0",
    });
  });
});
