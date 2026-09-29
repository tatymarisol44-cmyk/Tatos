package dev.agency.jvmagent.specialist;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import dev.agency.jvmagent.AgentProperties;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.messages.Message;

class LlmSpecialistTest {

    private ChatClient.ChatClientRequestSpec spec;
    private ChatClient.CallResponseSpec call;
    private ConversationMemory memory;
    private LlmSpecialist specialist;

    @BeforeEach
    void setUp() {
        ChatClient chat = mock(ChatClient.class);
        spec = mock(ChatClient.ChatClientRequestSpec.class);
        call = mock(ChatClient.CallResponseSpec.class);
        when(chat.prompt()).thenReturn(spec);
        when(spec.system(anyString())).thenReturn(spec);
        when(spec.messages(anyList())).thenReturn(spec);
        when(spec.user(anyString())).thenReturn(spec);
        when(spec.call()).thenReturn(call);
        ChatClient.Builder builder = mock(ChatClient.Builder.class);
        when(builder.build()).thenReturn(chat);
        memory = new ConversationMemory(new AgentProperties(null, null, null, 10, 10));
        specialist = new LlmSpecialist(builder, new RuleBasedSpecialist(), memory);
    }

    @Test
    void answersWithTheModelAndRemembersTheTurn() {
        when(call.content()).thenReturn("Use ZGC.");
        var answer = specialist.answer("GC pauses are too long", "ctx-1");
        assertThat(answer).isEqualTo(new Specialist.Answer("Use ZGC.", "llm"));
        assertThat(memory.history("ctx-1")).extracting(ConversationMemory.Turn::text)
                .containsExactly("GC pauses are too long", "Use ZGC.");
    }

    @Test
    @SuppressWarnings("unchecked")
    void groundsTheModelAndReplaysHistory() {
        memory.append("ctx-1", "earlier question", "earlier answer");
        when(call.content()).thenReturn("ok");
        specialist.answer("GC pauses are too long", "ctx-1");

        ArgumentCaptor<String> system = ArgumentCaptor.forClass(String.class);
        verify(spec).system(system.capture());
        assertThat(system.getValue())
                .startsWith(LlmSpecialist.SYSTEM)
                .contains("Reference notes:", "### Garbage collection");

        ArgumentCaptor<List<Message>> history = ArgumentCaptor.forClass(List.class);
        verify(spec).messages(history.capture());
        assertThat(history.getValue()).extracting(Message::getText)
                .containsExactly("earlier question", "earlier answer");
        verify(spec).user("GC pauses are too long");
    }

    @Test
    void omitsReferenceNotesWhenNoTopicMatches() {
        when(call.content()).thenReturn("ok");
        specialist.answer("hello", "ctx-4");
        verify(spec).system(LlmSpecialist.SYSTEM);
    }

    @Test
    void fallsBackToRulesWhenTheModelFails() {
        when(call.content()).thenThrow(new IllegalStateException("401 bad key"));
        var answer = specialist.answer("OutOfMemoryError", "ctx-2");
        assertThat(answer.mode()).isEqualTo("rules-fallback");
        assertThat(answer.text()).contains("HeapDumpOnOutOfMemoryError");
        assertThat(memory.history("ctx-2")).isEmpty();
    }

    @Test
    void blankCompletionCountsAsFailure() {
        when(call.content()).thenReturn("  ");
        assertThat(specialist.answer("hello", "ctx-3").mode()).isEqualTo("rules-fallback");
    }
}
