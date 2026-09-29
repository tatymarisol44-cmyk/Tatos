package dev.agency.jvmagent.a2a;

import dev.agency.jvmagent.AgentProperties;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.support.ServletUriComponentsBuilder;

/**
 * Serves the A2A Agent Card: who this agent is, where its JSON-RPC endpoint lives and which
 * skills it offers. The orchestrator indexes the description and skills for routing, so they
 * are written as a router would search for them.
 */
@RestController
public class AgentCardController {

    static final String NAME = "JVM Performance Specialist";
    static final String DESCRIPTION = "Diagnoses and tunes Java/JVM performance: "
            + "OutOfMemoryError and memory leaks, garbage collection pauses (G1, ZGC), JVM heap "
            + "sizing in Docker and Kubernetes (OOMKilled), startup time, threads and virtual "
            + "threads, and CPU profiling with JFR and async-profiler.";

    static final List<Map<String, Object>> SKILLS = List.of(
            skill("heap", "Heap and OutOfMemoryError diagnosis",
                    "Heap dumps, memory leaks, Metaspace and direct memory errors.",
                    "jvm", "java", "memory", "outofmemoryerror", "heap dump"),
            skill("containers", "JVM sizing in containers",
                    "MaxRAMPercentage, native memory, OOMKilled pods in Kubernetes.",
                    "docker", "kubernetes", "memory limits"),
            skill("gc", "Garbage collection tuning",
                    "GC logs, G1 pause targets, generational ZGC, allocation rate.",
                    "gc", "g1", "zgc", "latency"),
            skill("startup", "Startup time",
                    "AppCDS and the AOT cache, Spring AOT, CRaC, GraalVM native image.",
                    "startup", "spring boot", "graalvm"),
            skill("threads", "Threads and virtual threads",
                    "Thread dumps, deadlocks, virtual threads and pinning, pool sizing.",
                    "threads", "virtual threads", "concurrency"),
            skill("cpu", "CPU profiling",
                    "Java Flight Recorder, async-profiler flame graphs, JMH benchmarks.",
                    "cpu", "profiling", "jfr"));

    private final AgentProperties props;

    public AgentCardController(AgentProperties props) {
        this.props = props;
    }

    private static Map<String, Object> skill(
            String id, String name, String description, String... tags) {
        return Map.of("id", id, "name", name, "description", description, "tags", List.of(tags));
    }

    /** Advertised base URL: configured, or the one the caller used to reach us. */
    String baseUrl() {
        String base = props.publicUrl().isEmpty()
                ? ServletUriComponentsBuilder.fromCurrentContextPath().toUriString()
                : props.publicUrl();
        return base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
    }

    @GetMapping({"/.well-known/agent-card.json", "/.well-known/agent.json"})
    public Map<String, Object> card() {
        Map<String, Object> card = new LinkedHashMap<>();
        card.put("protocolVersion", "0.3.0");
        card.put("name", NAME);
        card.put("description", DESCRIPTION);
        card.put("url", baseUrl() + "/a2a");
        card.put("preferredTransport", "JSONRPC");
        card.put("version", "0.1.0");
        card.put("capabilities", Map.of("streaming", false, "pushNotifications", false));
        card.put("defaultInputModes", List.of("text/plain"));
        card.put("defaultOutputModes", List.of("text/plain"));
        if (!props.apiKey().isEmpty()) {
            card.put("securitySchemes", Map.of(
                    "apiKey", Map.of("type", "apiKey", "in", "header", "name", "X-API-Key")));
            card.put("security", List.of(Map.of("apiKey", List.of())));
        }
        card.put("skills", SKILLS);
        return card;
    }
}
