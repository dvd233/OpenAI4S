/**
 * Which GET the status block believes. Responses race: two reads can be in
 * flight at once (the menu opening and a hint), the conversation can be
 * switched or reopened under them (a branch activation reopens it), and a
 * selection save moves no event cursor at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _openGen, currentId } from "../../stores/session";
import { resetStoreFields } from "../../stores/signal-field";
import {
  autoModeStatus,
  beginAutoModeStatusRead,
  refreshAutoModeStatus,
  resetAutoModeStatus,
  syncAutoModeContext,
} from "./status";
import { autoModeBody, deferred, json, routeFetch, runBody, selectionBody, settle, type FetchCall } from "./testing";

type Pending = { call: FetchCall; respond: (response: Response) => void };

let pending: Pending[];
let calls: FetchCall[];

function body(ordinal: number | null, revision = 0, extra: Record<string, unknown> = {}) {
  return autoModeBody({
    last_event_ordinal: ordinal,
    selection: selectionBody({ revision }),
    run: ordinal ? runBody({ last_event_ordinal: ordinal }) : null,
    ...extra,
  });
}

function shownOrdinal(): number | null | undefined {
  const status = autoModeStatus();
  return status.phase === "ready" ? status.view.last_event_ordinal : undefined;
}

function shownRevision(): number | undefined {
  const status = autoModeStatus();
  return status.phase === "ready" ? status.view.selection.revision : undefined;
}

beforeEach(() => {
  resetStoreFields();
  resetAutoModeStatus();
  pending = [];
  calls = routeFetch((_url, call) => {
    const gate = deferred<Response>();
    pending.push({ call, respond: gate.resolve });
    return gate.promise;
  });
  currentId.value = "f-root";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ordering", () => {
  it("keeps the newer cursor when two reads that overlapped answer out of order", async () => {
    const first = refreshAutoModeStatus();
    const second = refreshAutoModeStatus();
    pending[1]!.respond(json(body(5)));
    await second;
    expect(shownOrdinal()).toBe(5);
    pending[0]!.respond(json(body(4)));
    await first;
    expect(shownOrdinal()).toBe(5);
  });

  it("takes the older-issued read when it carries the newer cursor", async () => {
    const first = refreshAutoModeStatus();
    const second = refreshAutoModeStatus();
    pending[1]!.respond(json(body(4)));
    await second;
    pending[0]!.respond(json(body(6)));
    await first;
    expect(shownOrdinal()).toBe(6);
  });

  it("accepts a newer selection revision when the event cursor did not move", async () => {
    const first = refreshAutoModeStatus();
    pending[0]!.respond(json(body(3, 0)));
    await first;
    const second = refreshAutoModeStatus();
    pending[1]!.respond(json(body(3, 1, { selection: selectionBody({ revision: 1, preset: "autonomous", result_review_mode: "auto_fix", approvals_reviewer: "auto_review", source: "frame", explicit: true }) })));
    await second;
    expect(shownRevision()).toBe(1);
    const status = autoModeStatus();
    expect(status.phase === "ready" && status.view.selection.preset).toBe("autonomous");
  });

  it("orders overlapping reads with one cursor by selection revision", async () => {
    const first = refreshAutoModeStatus();
    const second = refreshAutoModeStatus();
    pending[0]!.respond(json(body(3, 2)));
    await first;
    pending[1]!.respond(json(body(3, 1)));
    await second;
    expect(shownRevision()).toBe(2);
  });

  it("lets the later-issued read win a full tie, so an inherited change still lands", async () => {
    const first = refreshAutoModeStatus();
    const second = refreshAutoModeStatus();
    pending[1]!.respond(json(body(3, 0, { selection: selectionBody({ source: "legacy_result_review", explicit: true, result_review_mode: "review_only" }) })));
    await second;
    pending[0]!.respond(json(body(3, 0)));
    await first;
    const status = autoModeStatus();
    expect(status.phase === "ready" && status.view.selection.source).toBe("legacy_result_review");
  });

  it.each([false, true])("keeps the newer inherited project revision when reads cross (newer first: %s)", async (newerFirst) => {
    const first = refreshAutoModeStatus();
    const second = refreshAutoModeStatus();
    const projectBody = (sourceRevision: number) => body(3, 0, {
      selection: selectionBody({ source: "project", source_revision: sourceRevision }),
    });
    if (newerFirst) {
      pending[0]!.respond(json(projectBody(2)));
      await first;
      pending[1]!.respond(json(projectBody(1)));
      await second;
    } else {
      pending[1]!.respond(json(projectBody(1)));
      await second;
      pending[0]!.respond(json(projectBody(2)));
      await first;
    }
    const status = autoModeStatus();
    expect(status.phase === "ready" && status.view.selection.source_revision).toBe(2);
  });

  it("does not compare revisions from different selection sources", async () => {
    const first = refreshAutoModeStatus();
    const second = refreshAutoModeStatus();
    pending[0]!.respond(json(body(3, 0, { selection: selectionBody({ source: "project", source_revision: 9 }) })));
    await first;
    pending[1]!.respond(json(body(3, 0)));
    await second;
    const status = autoModeStatus();
    expect(status.phase === "ready" && status.view.selection.source).toBe("built_in_defaults");
  });

  it("believes a read issued after the shown one arrived, even with a lower cursor (a revert)", async () => {
    const first = refreshAutoModeStatus();
    pending[0]!.respond(json(body(9)));
    await first;
    const second = refreshAutoModeStatus();
    pending[1]!.respond(json(body(4)));
    await second;
    expect(shownOrdinal()).toBe(4);
  });

  it("does not let a stale success overwrite the failure of the latest read", async () => {
    const first = refreshAutoModeStatus();
    const second = refreshAutoModeStatus();
    pending[1]!.respond(json({ error: "down", code: "auto_mode_storage_unavailable" }, 503));
    await second;
    expect(autoModeStatus()).toMatchObject({ phase: "failed", failure: "unavailable" });
    pending[0]!.respond(json(body(2)));
    await first;
    expect(autoModeStatus().phase).toBe("failed");
  });
});

describe("menu openings", () => {
  it.each([
    ["success", "before"],
    ["503", "before"],
    ["success", "after"],
    ["503", "after"],
  ] as const)("ignores an earlier menu opening's %s arriving %s the current read", async (outcome, arrival) => {
    const previous = beginAutoModeStatusRead();
    expect(autoModeStatus()).toEqual({ phase: "loading", frameId: "f-root" });
    // The menu is reopened while its previous GET is still pending; neither
    // the conversation nor its branch-opening generation has changed.
    const reopened = beginAutoModeStatusRead();
    let whileCurrentPending = autoModeStatus();
    const previousResponse = () => outcome === "success"
      ? json(body(9, 4))
      : json({ error: "storage", code: "auto_mode_storage_unavailable" }, 503);
    if (arrival === "before") {
      pending[0]!.respond(previousResponse());
      await previous;
      whileCurrentPending = autoModeStatus();
    }
    pending[1]!.respond(json(body(3, 1)));
    await reopened;
    if (arrival === "after") {
      pending[0]!.respond(previousResponse());
      await previous;
    }
    expect(whileCurrentPending).toEqual({ phase: "loading", frameId: "f-root" });
    expect(autoModeStatus()).toMatchObject({
      phase: "ready",
      frameId: "f-root",
      view: { last_event_ordinal: 3, selection: { revision: 1 } },
    });
  });

  it("keeps cursor, selection and source revision ordering for hints within one reopened menu", async () => {
    const previous = beginAutoModeStatusRead();
    pending[0]!.respond(json(body(2)));
    await previous;
    const menu = beginAutoModeStatusRead();
    const cursorHint = refreshAutoModeStatus();
    const selectionHint = refreshAutoModeStatus();
    const sourceHint = refreshAutoModeStatus();
    const projectBody = (ordinal: number, revision: number, sourceRevision: number) => body(ordinal, revision, {
      selection: selectionBody({ source: "project", revision, source_revision: sourceRevision }),
    });
    pending[4]!.respond(json(projectBody(6, 2, 2)));
    await sourceHint;
    // The first-issued read has the newer inherited setting; later-issued
    // hints cannot replace it with an older cursor or selection revision.
    pending[1]!.respond(json(projectBody(6, 2, 3)));
    await menu;
    pending[3]!.respond(json(projectBody(6, 1, 9)));
    await selectionHint;
    pending[2]!.respond(json(projectBody(5, 9, 9)));
    await cursorHint;
    expect(autoModeStatus()).toMatchObject({
      phase: "ready",
      view: {
        last_event_ordinal: 6,
        selection: { revision: 2, source: "project", source_revision: 3 },
      },
    });
  });
});

describe("frame and branch changes", () => {
  it("drops a response for a conversation that is no longer open", async () => {
    const read = refreshAutoModeStatus();
    currentId.value = "f-other";
    syncAutoModeContext();
    pending[0]!.respond(json(body(5)));
    await read;
    expect(autoModeStatus()).toEqual({ phase: "loading", frameId: "f-other" });
  });

  it("drops a response for an earlier opening of the same conversation (a branch activation)", async () => {
    const read = refreshAutoModeStatus();
    _openGen.value += 1;
    pending[0]!.respond(json(body(5, 0, { branch_id: "branch-a" })));
    await read;
    expect(autoModeStatus().phase).not.toBe("ready");
    const fresh = refreshAutoModeStatus();
    pending[1]!.respond(json(body(2, 0, { branch_id: "branch-b" })));
    await fresh;
    const status = autoModeStatus();
    expect(status.phase === "ready" && status.view.branch_id).toBe("branch-b");
  });

  it("refuses a body for another root as unavailable rather than showing it", async () => {
    const read = refreshAutoModeStatus();
    pending[0]!.respond(json(body(5, 0, { root_frame_id: "f-someone-else" })));
    await read;
    expect(autoModeStatus()).toMatchObject({ phase: "failed", failure: "unavailable" });
  });

  it("shows loading on each menu read instead of the answer this tab cached", async () => {
    const first = refreshAutoModeStatus();
    pending[0]!.respond(json(body(5)));
    await first;
    expect(autoModeStatus().phase).toBe("ready");
    const menu = beginAutoModeStatusRead();
    expect(autoModeStatus()).toEqual({ phase: "loading", frameId: "f-root" });
    pending[1]!.respond(json(body(6)));
    await menu;
    expect(shownOrdinal()).toBe(6);
  });
});

describe("failures", () => {
  it.each([
    [json({ error: "frame not found", code: "frame_not_found" }, 404), "not_found"],
    [json({ error: "no route" }, 404), "unavailable"],
    [json({ error: "storage", code: "auto_mode_storage_unavailable" }, 503), "unavailable"],
    [json({ error: "boom" }, 500), "unavailable"],
    [json(autoModeBody({ schema_version: 2 })), "unavailable"],
  ])("maps %o to %s", async (response, failure) => {
    const read = refreshAutoModeStatus();
    pending[0]!.respond(response as Response);
    await read;
    expect(autoModeStatus()).toEqual({ phase: "failed", frameId: "f-root", failure });
  });

  it("treats a network failure as unavailable and recovers on the next read", async () => {
    vi.unstubAllGlobals();
    let fail = true;
    calls = routeFetch(() => {
      if (fail) throw new TypeError("Failed to fetch");
      return json(body(3));
    });
    await refreshAutoModeStatus();
    expect(autoModeStatus()).toMatchObject({ phase: "failed", failure: "unavailable" });
    fail = false;
    await refreshAutoModeStatus();
    expect(shownOrdinal()).toBe(3);
  });

  it("only ever reads", async () => {
    const reads = [refreshAutoModeStatus(), beginAutoModeStatusRead()];
    for (const item of pending) item.respond(json(body(1)));
    await Promise.all(reads);
    await settle();
    expect(calls.map((call) => [call.method, call.path])).toEqual([
      ["GET", "/api/v1/frames/f-root/auto-mode"],
      ["GET", "/api/v1/frames/f-root/auto-mode"],
    ]);
  });
});
