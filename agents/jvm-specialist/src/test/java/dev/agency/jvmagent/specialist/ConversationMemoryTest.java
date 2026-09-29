package dev.agency.jvmagent.specialist;

import static org.assertj.core.api.Assertions.assertThat;

import dev.agency.jvmagent.AgentProperties;
import org.junit.jupiter.api.Test;

class ConversationMemoryTest {

    @Test
    void keepsTheLastMessagesPerContext() {
        var memory = new ConversationMemory(new AgentProperties(null, null, null, 10, 4));
        memory.append("a", "q1", "a1");
        memory.append("a", "q2", "a2");
        memory.append("a", "q3", "a3");
        assertThat(memory.history("a")).extracting(ConversationMemory.Turn::text)
                .containsExactly("q2", "a2", "q3", "a3");
        assertThat(memory.history("unknown")).isEmpty();
    }

    @Test
    void evictsLeastRecentlyUsedContexts() {
        var memory = new ConversationMemory(new AgentProperties(null, null, null, 2, 10));
        memory.append("a", "q", "a");
        memory.append("b", "q", "a");
        memory.history("a"); // touch "a" so "b" becomes the eldest
        memory.append("c", "q", "a");
        assertThat(memory.size()).isEqualTo(2);
        assertThat(memory.history("b")).isEmpty();
        assertThat(memory.history("a")).hasSize(2);
    }

    @Test
    void defaultsApplyWhenPropertiesAreMissing() {
        var props = new AgentProperties(null, null, null, null, null);
        assertThat(props.publicUrl()).isEmpty();
        assertThat(props.apiKey()).isEmpty();
        assertThat(props.maxInputChars()).isEqualTo(8000);
        assertThat(props.maxContexts()).isEqualTo(1000);
        assertThat(props.maxMessagesPerContext()).isEqualTo(10);
    }
}
