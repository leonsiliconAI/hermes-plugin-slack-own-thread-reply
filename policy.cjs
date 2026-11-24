Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// policy.ts
function error(message) {
	return {
		success: false,
		error: message
	};
}
async function reply(host, { channel, thread, text }, allowed, channelEnv, limit, budget) {
	if (!/^[CG][A-Z0-9]{6,}$/.test(channel)) return error("channel must be a Slack channel ID such as C0THREADS01");
	if (!/^\d{9,11}\.\d{6}$/.test(thread)) return error("thread_ts must be a Slack message ts such as 1794360293.700699");
	if (!text) return error("text is required");
	const length = Array.from(text).length;
	if (length > limit) return error(`text is ${length} characters; keep a thread reply under ${limit}`);
	if (allowed.length && !allowed.includes(channel)) return error(`channel ${channel} is not in ${channelEnv}`);
	if (!host.available()) return error("no live Slack adapter in this process; this tool only runs inside the gateway");
	const { userId, botId, root, error: readError } = await host.readThread(channel, thread);
	if (readError) return error(readError);
	if (root?.ts !== thread) return error(`${thread} is not the top message of a thread in ${channel}; pass the thread's own ts`);
	if (root.user !== userId && (!botId || root.bot_id !== botId)) return error(`the top message of ${channel}:${thread} was not posted by Hermes's bot (${userId}); this tool only replies in threads Hermes started`);
	const stamp = host.now();
	if (!await host.reserve(channel, thread, stamp)) return error(`already sent ${budget} replies to this thread in the last hour; wait for an answer instead of pinging again`);
	let sent = false;
	try {
		const result = await host.send(`slack:${channel}:${thread}`, text);
		sent = typeof result === "object" && result !== null && "success" in result && Boolean(result.success);
		if (sent) host.logSend(channel, thread);
		return result;
	} finally {
		if (!sent) await host.release(channel, thread, stamp);
	}
}
function reserveReply(sent, now, budget) {
	const recent = sent.filter((stamp) => now - stamp < 3600);
	if (recent.length >= budget) return {
		allowed: false,
		recent
	};
	return {
		allowed: true,
		recent: [...recent, now]
	};
}
exports.reply = reply;
exports.reserveReply = reserveReply;
