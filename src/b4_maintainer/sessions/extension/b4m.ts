// SPDX-License-Identifier: GPL-2.0-or-later
/**
 * The b4-maintainer pi extension: loaded with `-e` into every b4-maintainer session.
 *
 * - Reports the session's state to the drawer (events.jsonl).
 * - Enforces the session's limits before every tool call (pi has no permissions of its
 *   own): shell commands are decided by b4-maintainer's guard (`b4-maintainer hook`);
 *   reads and searches stay within the session's readable directories; writes and edits
 *   go only to the session's output directory and worktree. A failed check blocks.
 * - Registers the scripted dry-run model (`b4m/dry-run`): no model or provider is called.
 *
 * - Adds `/back` and F12: return to b4-maintainer's pane, leaving the session running.
 *
 * The session is described by B4M_* environment variables (see sessions/pi.py); without
 * them the extension only registers the dry-run model.
 */

import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, join, resolve, sep } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createBashTool } from "@earendil-works/pi-coding-agent";
import {
	type Api,
	type AssistantMessage,
	type AssistantMessageEventStream,
	createAssistantMessageEventStream,
	getCurrentSystemPrompt,
	getCurrentTools,
	type Model,
	type SimpleStreamOptions,
	type TranscriptContext,
} from "@earendil-works/pi-ai";

const env = process.env;
const SESSION = env.B4M_SESSION ?? "";
const EVENTS = env.B4M_EVENTS ?? "";
const PERMISSIONS = env.B4M_PERMISSIONS ?? "read-only";
// The client found the sandbox usable at launch: every shell command runs in it.
const SANDBOXED = env.B4M_SANDBOX === "1";
const lines = (value: string | undefined) => (value ?? "").split("\n").filter((v) => v);

// ---------------------------------------------------------------------------------------
// State events

function now(): string {
	return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function record(event: string, tool = "", detail = ""): void {
	if (!EVENTS) return;
	const entry: Record<string, string> = { time: now(), event };
	if (tool) entry.tool = tool;
	if (detail) entry.detail = detail.slice(0, 200);
	try {
		appendFileSync(EVENTS, JSON.stringify(entry) + "\n");
	} catch {
		// The drawer misses one event; the session goes on.
	}
}

// ---------------------------------------------------------------------------------------
// Limits

function real(path: string): string {
	// Resolve symlinks (a series may add one pointing anywhere); for a file that does not
	// exist yet, resolve its nearest existing directory.
	let probe = path;
	const rest: string[] = [];
	while (!existsSync(probe) && dirname(probe) !== probe) {
		rest.unshift(probe.slice(dirname(probe).length + 1));
		probe = dirname(probe);
	}
	try {
		return join(realpathSync(probe), ...rest);
	} catch {
		return path;
	}
}

function within(path: string, roots: string[]): boolean {
	const target = real(path);
	return roots.some((root) => {
		const r = real(root);
		return target === r || target.startsWith(r.endsWith(sep) ? r : r + sep);
	});
}

function absolute(path: unknown, cwd: string): string {
	const p = typeof path === "string" && path ? path : ".";
	return isAbsolute(p) ? p : resolve(cwd, p);
}

const READ_TOOLS = new Set(["read", "grep", "find", "ls"]);
const WRITE_TOOLS = new Set(["write", "edit"]);

function writable(): string[] {
	// Artifacts go under out/ in every session; worktree (execute) sessions also edit their tree.
	const worktree = PERMISSIONS === "read-only" ? "" : (env.B4M_WORKTREE ?? "");
	return [env.B4M_OUT ?? "", worktree].filter((v) => v);
}

function guardShell(command: string): string | undefined {
	// b4-maintainer's guard decides; it also records the tool event.
	const hook = env.B4M_HOOK || "b4-maintainer";
	const payload = JSON.stringify({
		event: "tool",
		tool: "bash",
		input: { command },
		permissions: PERMISSIONS,
		readable: lines(env.B4M_READABLE), // Read-only git may name these with -C.
	});
	const run = spawnSync(hook, ["hook", "-C", env.B4M_ROOT ?? ".", SESSION], {
		input: payload,
		encoding: "utf8",
		timeout: 10000,
	});
	if (run.error || run.signal || (run.status !== 0 && run.status !== 1)) {
		return `b4-maintainer: the command check could not run (${run.error?.message ?? run.signal ?? run.status})`;
	}
	const out = (run.stdout ?? "").trim();
	if (!out) return undefined;
	try {
		const decision = JSON.parse(out);
		return decision.decision === "deny" ? String(decision.reason) : undefined;
	} catch {
		return "b4-maintainer: the command check answered unreadably";
	}
}

function check(tool: string, input: any, cwd: string): string | undefined {
	if (tool === "bash") return SANDBOXED ? undefined : guardShell(String(input?.command ?? ""));
	if (READ_TOOLS.has(tool)) {
		const path = absolute(input?.path, cwd);
		if (!within(path, lines(env.B4M_READABLE))) {
			return `b4-maintainer: ${path} is outside the session's readable directories`;
		}
		return undefined;
	}
	if (WRITE_TOOLS.has(tool)) {
		const path = absolute(input?.path, cwd);
		if (!within(path, writable())) {
			return `b4-maintainer: this session may write only under ${writable().join(" and ")}`;
		}
		return undefined;
	}
	return undefined; // Other tools are not enabled for sessions (--tools).
}

// ---------------------------------------------------------------------------------------
// The dry-run model: a script instead of a model

type Step = { tool: string; args: Record<string, unknown> } | { text: string };

function defaultSteps(): Step[] {
	const context = EVENTS ? join(dirname(EVENTS), "work", "context", "task.md") : "task.md";
	return [
		{ tool: "read", args: { path: context } },
		{ tool: "bash", args: { command: "git log -1 --oneline" } },
		{ tool: "bash", args: { command: "make -j8" } },
	];
}

function steps(): Step[] {
	try {
		return env.B4M_DRY_RUN_STEPS ? JSON.parse(env.B4M_DRY_RUN_STEPS) : defaultSteps();
	} catch {
		return defaultSteps();
	}
}

function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (Array.isArray(content)) return content.map((c: any) => (typeof c?.text === "string" ? c.text : "")).join("");
	return "";
}

function summary(messages: any[]): string {
	const system = getCurrentSystemPrompt(messages) ?? "";
	const tools = getCurrentTools(messages).map((t: any) => t.name);
	const task = textOf(messages.filter((m) => m.role === "user").pop()?.content).slice(0, 500);
	return [
		"Dry run: no model is called. A real model would receive:",
		`- task: ${task}`,
		`- tools: ${tools.join(", ")}`,
		`- system prompt: ${system.length} characters, ending:`,
		system.slice(-600),
		"It now makes the tool calls a model might: reading the task, a read-only git command, and a build the shell check refuses.",
	].join("\n");
}

function dryRun(model: Model<Api>, context: TranscriptContext, options?: SimpleStreamOptions): AssistantMessageEventStream {
	const stream = createAssistantMessageEventStream();
	const messages = context.messages as any[];
	let lastUser = -1;
	messages.forEach((m, i) => {
		if (m.role === "user") lastUser = i;
	});
	const firstTurn = messages.filter((m) => m.role === "user").length === 1;
	const done = messages.slice(lastUser + 1).filter((m) => m.role === "toolResult").length;
	const script = steps();
	(async () => {
		const output = {
			role: "assistant",
			content: [] as any[],
			api: model.api,
			provider: model.provider,
			model: model.id,
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "pending",
			timestamp: Date.now(),
		} as unknown as AssistantMessage;
		const content = output.content as any[];
		stream.push({ type: "start", partial: output } as any);
		const text = (value: string) => {
			content.push({ type: "text", text: value });
			const index = content.length - 1;
			stream.push({ type: "text_start", contentIndex: index, partial: output } as any);
			stream.push({ type: "text_delta", contentIndex: index, delta: value, partial: output } as any);
			stream.push({ type: "text_end", contentIndex: index, content: value, partial: output } as any);
		};
		if (!firstTurn) {
			const said = textOf(messages[lastUser]?.content);
			text(`Dry run: no model is called. You said: ${said}`);
			(output as any).stopReason = "stop";
		} else if (done < script.length) {
			if (done === 0 && !env.B4M_DRY_RUN_STEPS) text(summary(messages));
			const step = script[done];
			if ("text" in step) {
				text(step.text);
				(output as any).stopReason = "stop";
			} else {
				const call = { type: "toolCall", id: `b4m_${Date.now()}_${done}`, name: step.tool, arguments: step.args };
				content.push(call);
				const index = content.length - 1;
				stream.push({ type: "toolcall_start", contentIndex: index, partial: output } as any);
				stream.push({ type: "toolcall_end", contentIndex: index, toolCall: call, partial: output } as any);
				(output as any).stopReason = "toolUse";
			}
		} else {
			text("Dry run finished. The refused build shows how b4-maintainer's shell check answers a model. Type anything for an echo.");
			(output as any).stopReason = "stop";
		}
		stream.push({ type: "done", reason: (output as any).stopReason, message: output } as any);
		(stream as any).end?.();
	})();
	return stream;
}

// ---------------------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
	pi.registerProvider("b4m", {
		baseUrl: "http://127.0.0.1:9",
		apiKey: "none",
		api: "b4m-dry-run",
		models: [
			{
				id: "dry-run",
				name: "b4-maintainer dry run (scripted, no model)",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 100000,
				maxTokens: 8000,
			},
		],
		streamSimple: dryRun,
	} as any);

	if (!SESSION) return; // Not a b4-maintainer session: nothing to report or enforce.

	// Back to b4-maintainer, leaving this session running: the app announces its pane as a
	// tmux option at startup (read now, so it survives app restarts); the window given at
	// launch is the fallback.
	const backToApp = (ctx: any) => {
		const tmux = (...args: string[]) => spawnSync("tmux", args, { encoding: "utf8" });
		const pane = (tmux("display-message", "-p", "#{@b4m_app_pane}").stdout ?? "").trim();
		const target = pane || (env.B4M_APP_WINDOW ?? "");
		if (!target) {
			ctx?.ui?.notify?.("b4-maintainer: no app window known", "warning");
			return;
		}
		const run = tmux("select-window", "-t", target);
		tmux("switch-client", "-t", target);
		if (pane) tmux("select-pane", "-t", pane);
		if (run.status !== 0) ctx?.ui?.notify?.(`tmux: ${run.stderr || run.error?.message}`, "warning");
	};
	pi.registerCommand("back", {
		description: "Back to b4-maintainer; this session keeps running",
		handler: async (_args: string, ctx: any) => backToApp(ctx),
	});
	pi.registerShortcut("f12" as any, {
		description: "Back to b4-maintainer (F12 there returns here)",
		handler: async (ctx: any) => backToApp(ctx),
	});

	if (SANDBOXED) {
		// pi's bash, with each command run by `b4-maintainer sandbox`: the command travels in
		// the environment, so no shell quoting stands between the model and the sandbox.
		const quote = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`;
		const wrapper = [env.B4M_HOOK || "b4-maintainer", "sandbox", "-C", env.B4M_ROOT ?? ".", SESSION];
		const bash = createBashTool(process.cwd(), {
			spawnHook: ({ command, cwd, env: commandEnv }: any) => ({
				command: `exec ${wrapper.map(quote).join(" ")}`,
				cwd,
				env: { ...commandEnv, B4M_SANDBOX_COMMAND: command },
			}),
		});
		pi.registerTool({
			...bash,
			execute: async (id: string, params: any, signal: any, onUpdate: any) => bash.execute(id, params, signal, onUpdate),
		} as any);
	}

	pi.on("session_start", () => record("start"));
	pi.on("input", () => record("working"));
	pi.on("agent_start", () => record("working"));
	pi.on("tool_result", (event: any) => record("tool_done", event.toolName ?? ""));
	pi.on("agent_settled", () => record("waiting"));
	pi.on("model_select", (event: any) => {
		const model = event?.model;
		record("model", "", model ? `${model.provider}/${model.id}` : "");
	});
	pi.on("session_shutdown", () => record("exit"));

	pi.on("tool_call", async (event: any, ctx: any) => {
		const tool = String(event.toolName ?? "");
		if (tool !== "bash" || SANDBOXED) record("tool", tool); // Else the hook records it.
		const reason = check(tool, event.input, ctx?.cwd ?? process.cwd());
		return reason ? { block: true, reason } : undefined;
	});
}
