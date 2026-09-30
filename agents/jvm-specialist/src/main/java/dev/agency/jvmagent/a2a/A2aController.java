package dev.agency.jvmagent.a2a;

import dev.agency.jvmagent.AgentProperties;
import dev.agency.jvmagent.specialist.Specialist;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

/**
 * A2A JSON-RPC 2.0 endpoint. Implements {@code message/send} with a text-only Message reply.
 *
 * <p>Protocol errors use the JSON-RPC codes: -32700 parse error, -32600 invalid request, -32601
 * method not found, -32602 invalid params, -32603 internal error. Authentication failures are
 * HTTP 401 before any JSON-RPC processing.
 */
@RestController
public class A2aController {

    private static final Logger log = LoggerFactory.getLogger(A2aController.class);
    private static final int MAX_CONTEXT_ID = 128;

    private final Specialist specialist;
    private final AgentProperties props;

    public A2aController(Specialist specialist, AgentProperties props) {
        this.specialist = specialist;
        this.props = props;
    }

    static Map<String, Object> error(Object id, int code, String message) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("jsonrpc", "2.0");
        body.put("id", id);
        body.put("error", Map.of("code", code, "message", message));
        return body;
    }

    private boolean authorized(String key) {
        if (props.apiKey().isEmpty()) {
            return true;
        }
        // Constant-time comparison: no timing side channel on the key.
        return key != null && MessageDigest.isEqual(
                key.getBytes(StandardCharsets.UTF_8),
                props.apiKey().getBytes(StandardCharsets.UTF_8));
    }

    @PostMapping(path = "/a2a", produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<Map<String, Object>> rpc(
            @RequestHeader(value = "X-API-Key", required = false) String apiKey,
            @RequestBody Object body) {
        if (!authorized(apiKey)) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(error(null, -32001, "Unauthorized"));
        }
        if (!(body instanceof Map<?, ?> request)) {
            return ResponseEntity.ok(error(null, -32600, "Invalid Request"));
        }
        Object id = request.get("id");
        if (id != null && !(id instanceof String) && !(id instanceof Number)) {
            return ResponseEntity.ok(error(null, -32600, "Invalid Request: bad id"));
        }
        if (!"2.0".equals(request.get("jsonrpc")) || !(request.get("method") instanceof String)) {
            return ResponseEntity.ok(error(id, -32600, "Invalid Request"));
        }
        if (!"message/send".equals(request.get("method"))) {
            return ResponseEntity.ok(error(id, -32601, "Method not found: " + request.get("method")));
        }
        Map<?, ?> message = request.get("params") instanceof Map<?, ?> params
                && params.get("message") instanceof Map<?, ?> m ? m : Map.of();
        String text = textParts(message);
        if (text.isEmpty()) {
            return ResponseEntity.ok(
                    error(id, -32602, "message must contain at least one text part"));
        }
        if (text.length() > props.maxInputChars()) {
            return ResponseEntity.ok(error(
                    id, -32602, "message exceeds " + props.maxInputChars() + " characters"));
        }
        String contextId = message.get("contextId") instanceof String c
                && !c.isBlank() && c.length() <= MAX_CONTEXT_ID ? c : UUID.randomUUID().toString();

        Specialist.Answer answer;
        try {
            answer = specialist.answer(text, contextId);
        } catch (RuntimeException e) {
            log.error("specialist failed", e);
            return ResponseEntity.ok(error(id, -32603, "Internal error"));
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("kind", "message");
        result.put("role", "agent");
        result.put("messageId", UUID.randomUUID().toString());
        result.put("contextId", contextId);
        result.put("parts", List.of(Map.of("kind", "text", "text", answer.text())));
        result.put("metadata", Map.of("mode", answer.mode()));
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("jsonrpc", "2.0");
        response.put("id", id);
        response.put("result", result);
        return ResponseEntity.ok(response);
    }

    private static String textParts(Map<?, ?> message) {
        if (!(message.get("parts") instanceof List<?> parts)) {
            return "";
        }
        return parts.stream()
                .filter(p -> p instanceof Map<?, ?> part
                        && "text".equals(part.get("kind"))
                        && part.get("text") instanceof String)
                .map(p -> (String) ((Map<?, ?>) p).get("text"))
                .collect(Collectors.joining("\n"))
                .strip();
    }

    /**
     * Malformed JSON is a JSON-RPC parse error, not an HTTP 400; a body cut off by {@link
     * BodySizeLimitFilter} is HTTP 413.
     */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<Map<String, Object>> parseError(HttpMessageNotReadableException e) {
        for (Throwable t = e; t != null; t = t.getCause()) {
            if (t instanceof BodySizeLimitFilter.PayloadTooLargeException) {
                return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE)
                        .body(error(null, -32600, "Request body too large"));
            }
        }
        return ResponseEntity.ok(error(null, -32700, "Parse error"));
    }
}
