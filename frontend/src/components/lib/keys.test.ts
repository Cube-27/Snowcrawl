import { describe, expect, it } from "vite-plus/test";
import { keyedTexts } from "./keys";

describe("keyedTexts", () => {
  it("keeps the texts in order, keying equal ones apart", () => {
    expect(keyedTexts(["red", "blue", "red"])).toEqual([
      { key: "0:red", text: "red" },
      { key: "0:blue", text: "blue" },
      { key: "1:red", text: "red" },
    ]);
  });
});
