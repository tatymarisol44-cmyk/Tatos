package dev.agency.jvmagent.specialist;

import dev.agency.jvmagent.AgentProperties;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Component;

/**
 * Per-{@code contextId} conversation history, bounded on both axes: at most {@code maxContexts}
 * conversations (least recently used evicted) and {@code maxMessagesPerContext} messages each,
 * so memory cannot grow without limit however many callers there are.
 *
 * <p>In-process by design: this is a single-replica demo agent. Scale-out would move it to
 * Redis, as the orchestrator does with its Postgres checkpointer.
 */
@Component
public class ConversationMemory {

    public record Turn(String role, String text) {}

    private final int maxMessages;
    private final Map<String, Deque<Turn>> contexts;

    public ConversationMemory(AgentProperties props) {
        this.maxMessages = props.maxMessagesPerContext();
        int maxContexts = props.maxContexts();
        this.contexts = new LinkedHashMap<>(16, 0.75f, true) {
            @Override
            protected boolean removeEldestEntry(Map.Entry<String, Deque<Turn>> eldest) {
                return size() > maxContexts;
            }
        };
    }

    public synchronized List<Turn> history(String contextId) {
        Deque<Turn> turns = contexts.get(contextId);
        return turns == null ? List.of() : List.copyOf(turns);
    }

    public synchronized void append(String contextId, String question, String answer) {
        Deque<Turn> turns = contexts.computeIfAbsent(contextId, k -> new ArrayDeque<>());
        turns.addLast(new Turn("user", question));
        turns.addLast(new Turn("assistant", answer));
        while (turns.size() > maxMessages) {
            turns.removeFirst();
        }
    }

    public synchronized int size() {
        return contexts.size();
    }
}
