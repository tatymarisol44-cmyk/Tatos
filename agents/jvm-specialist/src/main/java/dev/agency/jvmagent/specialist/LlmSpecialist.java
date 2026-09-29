package dev.agency.jvmagent.specialist;

import java.util.ArrayList;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

/**
 * LLM mode (Spring AI), active when {@code spring.ai.model.chat=anthropic} (environment {@code
 * AGENT_LLM_PROVIDER=anthropic} plus {@code ANTHROPIC_API_KEY}).
 *
 * <p>The rule engine's playbook for the question is passed as reference notes, which grounds
 * the model in vetted advice. If the model call fails, the rule engine answers instead, so the
 * agent degrades rather than erroring, the same policy the orchestrator applies.
 */
@Component
@Primary
@ConditionalOnProperty(name = "spring.ai.model.chat", havingValue = "anthropic")
public class LlmSpecialist implements Specialist {

    private static final Logger log = LoggerFactory.getLogger(LlmSpecialist.class);

    static final String SYSTEM = """
            You are a senior JVM performance engineer. Diagnose Java memory, garbage collection, \
            container sizing, startup, concurrency and CPU problems. Be concrete: name the JVM \
            flags, jcmd/JFR commands and trade-offs. If key facts are missing (JDK version, \
            flags, limits, symptoms), state your assumptions and say what to collect next. \
            Use the reference notes when they apply; do not invent flags or JEP numbers.""";

    private final ChatClient chat;
    private final RuleBasedSpecialist rules;
    private final ConversationMemory memory;

    public LlmSpecialist(
            ChatClient.Builder builder, RuleBasedSpecialist rules, ConversationMemory memory) {
        this.chat = builder.build();
        this.rules = rules;
        this.memory = memory;
    }

    @Override
    public Answer answer(String question, String contextId) {
        List<Message> history = new ArrayList<>();
        for (ConversationMemory.Turn turn : memory.history(contextId)) {
            history.add("user".equals(turn.role())
                    ? new UserMessage(turn.text())
                    : new AssistantMessage(turn.text()));
        }
        String notes = rules.notes(question);
        String system = notes.isEmpty() ? SYSTEM : SYSTEM + "\n\nReference notes:\n" + notes;
        try {
            String text = chat.prompt().system(system).messages(history).user(question).call()
                    .content();
            if (text == null || text.isBlank()) {
                throw new IllegalStateException("empty completion");
            }
            memory.append(contextId, question, text);
            return new Answer(text, "llm");
        } catch (RuntimeException e) {
            log.warn("LLM call failed ({}); answering with the rule engine", e.toString());
            Answer fallback = rules.answer(question, contextId);
            return new Answer(fallback.text(), "rules-fallback");
        }
    }
}
