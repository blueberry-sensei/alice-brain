import { describe, expect, it } from "vitest";

import { serverErrorMessage } from "./client-errors";

describe("serverErrorMessage", () => {
  // The CJK fixtures below are LEGACY server messages, written as escapes on purpose:
  // serverErrorMessage must replace anything not in the interface language with a localized fallback,
  // and that safety net still has to work for an older backend or older stored data.
  it("passes server details through untouched in the Vietnamese interface", () => {
    expect(serverErrorMessage("not_found", "Không tìm thấy tài liệu", 404, "vi-VN"))
      .toBe("Không tìm thấy tài liệu");
  });

  it("preserves server details that already match the English interface", () => {
    expect(serverErrorMessage("not_found", "Document not found", 404, "en-US"))
      .toBe("Document not found");
  });

  it("uses the error code to replace untranslated implementation text", () => {
    expect(serverErrorMessage("configuration_error", "\u5c1a\u672a\u914d\u7f6e LLM", 400, "en-US"))
      .toBe("The service is not fully configured. Complete the required settings first.");
  });

  it("does not leak Chinese implementation text into the Vietnamese interface", () => {
    expect(serverErrorMessage("configuration_error", "\u5c1a\u672a\u914d\u7f6e LLM", 400, "vi-VN"))
      .toBe("Dịch vụ chưa được cấu hình đầy đủ. Hãy hoàn tất phần cài đặt cần thiết.");
  });

  it("falls back to the HTTP status when the code is not specific", () => {
    expect(serverErrorMessage("error", "\u4fe1\u606f\u6e90\u4e0d\u5b58\u5728", 404, "en-US"))
      .toBe("The requested resource was not found");
  });
});
