package dev.agency.jvmagent;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Agent settings, bound from {@code agent.*} (environment: {@code AGENT_*}).
 *
 * @param publicUrl base URL advertised in the Agent Card; blank means "derive it from the
 *     request", which is right whenever callers reach the agent directly
 * @param apiKey when set, {@code /a2a} requires it in the {@code X-API-Key} header
 * @param maxInputChars longest accepted question
 * @param maxContexts conversations kept in memory (least recently used are evicted)
 * @param maxMessagesPerContext messages kept per conversation for the LLM mode
 */
@ConfigurationProperties(prefix = "agent")
public record AgentProperties(
        String publicUrl,
        String apiKey,
        Integer maxInputChars,
        Integer maxContexts,
        Integer maxMessagesPerContext) {

    public AgentProperties {
        publicUrl = publicUrl == null ? "" : publicUrl.strip();
        apiKey = apiKey == null ? "" : apiKey;
        maxInputChars = maxInputChars == null ? 8000 : maxInputChars;
        maxContexts = maxContexts == null ? 1000 : maxContexts;
        maxMessagesPerContext = maxMessagesPerContext == null ? 10 : maxMessagesPerContext;
    }
}
