package dev.agency.jvmagent.a2a;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import tools.jackson.databind.json.JsonMapper;

/** Speaks A2A over real HTTP to the running app, as the orchestrator does. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class A2aProtocolTest {

    static final JsonMapper JSON = JsonMapper.builder().build();
    static final HttpClient HTTP = HttpClient.newHttpClient();

    @LocalServerPort
    int port;

    HttpResponse<String> send(String method, String path, String body, String apiKey)
            throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header("Content-Type", "application/json");
        if (apiKey != null) {
            request.header("X-API-Key", apiKey);
        }
        request.method(method, body == null
                ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofString(body));
        return HTTP.send(request.build(), HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    Map<String, Object> rpc(String body) throws Exception {
        var response = send("POST", "/a2a", body, null);
        assertThat(response.statusCode()).isEqualTo(200);
        return JSON.readValue(response.body(), Map.class);
    }

    static String messageSend(Object id, String text, String contextId) {
        var message = new java.util.LinkedHashMap<String, Object>();
        message.put("kind", "message");
        message.put("role", "user");
        message.put("messageId", "m1");
        message.put("parts", List.of(Map.of("kind", "text", "text", text)));
        if (contextId != null) {
            message.put("contextId", contextId);
        }
        return JSON.writeValueAsString(Map.of(
                "jsonrpc", "2.0", "id", id, "method", "message/send",
                "params", Map.of("message", message)));
    }

    @SuppressWarnings("unchecked")
    static int errorCode(Map<String, Object> response) {
        return (Integer) ((Map<String, Object>) response.get("error")).get("code");
    }

    @Test
    @SuppressWarnings("unchecked")
    void agentCardDescribesTheAgent() throws Exception {
        var response = send("GET", "/.well-known/agent-card.json", null, null);
        assertThat(response.statusCode()).isEqualTo(200);
        Map<String, Object> card = JSON.readValue(response.body(), Map.class);
        assertThat(card)
                .containsEntry("name", "JVM Performance Specialist")
                .containsEntry("preferredTransport", "JSONRPC")
                .containsEntry("url", "http://localhost:" + port + "/a2a")
                .doesNotContainKey("securitySchemes");
        assertThat((List<Map<String, Object>>) card.get("skills"))
                .extracting(s -> s.get("id"))
                .containsExactly("heap", "containers", "gc", "startup", "threads", "cpu");
        assertThat(send("GET", "/.well-known/agent.json", null, null).statusCode()).isEqualTo(200);
    }

    @Test
    @SuppressWarnings("unchecked")
    void messageSendAnswersAndKeepsTheContext() throws Exception {
        var response = rpc(messageSend("req-1", "Pods get OOMKilled", "ctx-42"));
        assertThat(response).containsEntry("jsonrpc", "2.0").containsEntry("id", "req-1");
        var result = (Map<String, Object>) response.get("result");
        assertThat(result)
                .containsEntry("kind", "message")
                .containsEntry("role", "agent")
                .containsEntry("contextId", "ctx-42")
                .containsEntry("metadata", Map.of("mode", "rules"));
        var parts = (List<Map<String, Object>>) result.get("parts");
        assertThat((String) parts.getFirst().get("text")).contains("MaxRAMPercentage");
    }

    @Test
    @SuppressWarnings("unchecked")
    void generatesAContextIdAndEchoesNumericIds() throws Exception {
        var response = rpc(messageSend(7, "GC pauses", null));
        assertThat(response.get("id")).isEqualTo(7);
        var result = (Map<String, Object>) response.get("result");
        assertThat((String) result.get("contextId")).hasSize(36);
    }

    @Test
    void rejectsProtocolErrors() throws Exception {
        assertThat(errorCode(rpc("{not json"))).isEqualTo(-32700);
        assertThat(errorCode(rpc("[1, 2]"))).isEqualTo(-32600);
        assertThat(errorCode(rpc("{\"id\": 1, \"method\": \"message/send\"}"))).isEqualTo(-32600);
        assertThat(errorCode(rpc("{\"jsonrpc\": \"2.0\", \"id\": {}, \"method\": \"x\"}")))
                .isEqualTo(-32600);
        var unknown = rpc("{\"jsonrpc\": \"2.0\", \"id\": 3, \"method\": \"tasks/get\"}");
        assertThat(errorCode(unknown)).isEqualTo(-32601);
        assertThat(unknown.get("id")).isEqualTo(3);
        assertThat(errorCode(rpc("{\"jsonrpc\": \"2.0\", \"id\": 4, \"method\": \"message/send\"}")))
                .isEqualTo(-32602);
        assertThat(errorCode(rpc(messageSend(5, "   ", null)))).isEqualTo(-32602);
        assertThat(errorCode(rpc(messageSend(6, "x".repeat(8001), null)))).isEqualTo(-32602);
    }

    @Test
    @SuppressWarnings("unchecked")
    void overlongContextIdsAreReplaced() throws Exception {
        var response = rpc(messageSend(8, "GC pauses", "c".repeat(129)));
        var result = (Map<String, Object>) response.get("result");
        assertThat((String) result.get("contextId")).hasSize(36);
    }

    @Test
    void healthProbeIsExposed() throws Exception {
        var response = send("GET", "/actuator/health/readiness", null, null);
        assertThat(response.statusCode()).isEqualTo(200);
        assertThat(response.body()).contains("UP");
    }
}
