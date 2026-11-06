import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JdRequirements } from "../api/types";
import { ADMIN, mock, renderApp, seed, signIn } from "../test/utils";

/** JD review shows how requirements were extracted (REQ-MAT-02); Confirm & Start stays a manual step. */
describe("JD review extraction method", () => {
  beforeEach(async () => {
    await seed();
    await signIn("Admin", ADMIN.email, ADMIN.password);
  });
  afterEach(() => vi.restoreAllMocks());

  const open = async (req: Partial<JdRequirements>) => {
    const base = await mock.getRequirements(1);
    vi.spyOn(mock, "getRequirements").mockResolvedValue({ ...base, ...req });
    const confirm = vi.spyOn(mock, "confirmRequirements");
    renderApp("/demands");
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "Review JD" }))[0],
    );
    await screen.findByRole("button", { name: /Confirm & Start/ });
    return confirm;
  };

  it("warns with the backend reason on a rule-based fallback and hides the duplicate warning", async () => {
    const confirm = await open({
      extractionMethod: "rule_based_fallback",
      fallbackReason: "schema_invalid",
      warnings: ["Rule-based fallback: schema_invalid"],
    });
    expect(
      screen.getByText("Extracted by rules, not the local AI model"),
    ).toBeTruthy();
    expect(screen.getByText(/reason: schema_invalid/)).toBeTruthy();
    expect(
      screen.getByText(
        /Check every requirement carefully before Confirm & Start/,
      ),
    ).toBeTruthy();
    expect(
      screen.queryByText("Rule-based fallback: schema_invalid"),
    ).toBeNull();
    expect(screen.queryByText("Extracted by local AI model")).toBeNull();
    expect(confirm).not.toHaveBeenCalled();
  });

  it("shows a quiet note when the local model extracted the requirements", async () => {
    await open({ extractionMethod: "llm", fallbackReason: null });
    expect(screen.getByText("Extracted by local AI model")).toBeTruthy();
    expect(
      screen.queryByText("Extracted by rules, not the local AI model"),
    ).toBeNull();
  });

  it("shows nothing extra for older rows without an extraction method", async () => {
    await open({ extractionMethod: null, fallbackReason: null });
    expect(screen.queryByText("Extracted by local AI model")).toBeNull();
    expect(
      screen.queryByText("Extracted by rules, not the local AI model"),
    ).toBeNull();
  });

  it("mock demand 2 falls back to rules with a reason code", async () => {
    expect(await mock.getRequirements(2)).toMatchObject({
      extractionMethod: "rule_based_fallback",
      fallbackReason: "provider_unavailable",
    });
  });
});
