package dev.agency.jvmagent.a2a;

import static org.assertj.core.api.Assertions.assertThat;

import dev.agency.jvmagent.specialist.Specialist;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;

@SpringBootTest(
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"agent.api-key=s3cret", "agent.public-url=https://jvm.example.com/"})
class A2aSecurityTest extends A2aProtocolTest {

    /** A specialist that crashes on demand, to check internal errors do not leak. */
    @TestConfiguration
    static class Crashing {
        @Bean
        @Primary
        Specialist crashing() {
            return (question, contextId) -> {
                if (question.contains("crash")) {
                    throw new IllegalStateException("secret stack detail");
                }
                return new Specialist.Answer("fine", "test");
            };
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    Map<String, Object> rpc(String body) throws Exception {
        var response = send("POST", "/a2a", body, "s3cret");
        assertThat(response.statusCode()).isEqualTo(200);
        return JSON.readValue(response.body(), Map.class);
    }

    @Test
    void requiresTheApiKey() throws Exception {
        String body = messageSend(1, "GC pauses", null);
        assertThat(send("POST", "/a2a", body, null).statusCode()).isEqualTo(401);
        var wrong = send("POST", "/a2a", body, "s3creT");
        assertThat(wrong.statusCode()).isEqualTo(401);
        assertThat(wrong.body()).contains("-32001").doesNotContain("s3cret");
        assertThat(send("POST", "/a2a", body, "s3cret").statusCode()).isEqualTo(200);
    }

    @Override
    @Test
    @SuppressWarnings("unchecked")
    void agentCardDescribesTheAgent() throws Exception {
        // The card stays public (discovery), advertises the scheme and the configured URL.
        var response = send("GET", "/.well-known/agent-card.json", null, null);
        assertThat(response.statusCode()).isEqualTo(200);
        Map<String, Object> card = JSON.readValue(response.body(), Map.class);
        assertThat(card).containsEntry("url", "https://jvm.example.com/a2a");
        assertThat(card).containsKey("securitySchemes");
        assertThat((List<Object>) card.get("security")).hasSize(1);
    }

    @Override
    @Test
    @SuppressWarnings("unchecked")
    void messageSendAnswersAndKeepsTheContext() throws Exception {
        var result = (Map<String, Object>) rpc(messageSend("r", "hi", "ctx")).get("result");
        assertThat(result).containsEntry("metadata", Map.of("mode", "test"));
    }

    @Test
    void internalErrorsDoNotLeakDetails() throws Exception {
        var response = rpc(messageSend(9, "please crash", null));
        assertThat(errorCode(response)).isEqualTo(-32603);
        assertThat(response.toString()).doesNotContain("secret stack detail");
    }
}
