package dev.agency.jvmagent.specialist;

import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import org.springframework.stereotype.Component;

/**
 * Deterministic JVM performance advisor: matches the question against topics (English and
 * Spanish keywords) and returns the playbook for the best matches. It needs no API key, so
 * the agent is useful and testable offline, and it grounds and backs up the LLM mode.
 */
@Component
public class RuleBasedSpecialist implements Specialist {

    static final int MAX_TOPICS = 3;

    /**
     * A playbook section. Keywords match at a word start, so prefixes work ("profil" matches
     * "profiler") but "oom" does not match "room".
     */
    record Topic(String id, List<String> keywords, String advice, List<Pattern> patterns) {
        Topic(String id, List<String> keywords, String advice) {
            // Compiled once at class load, not per request.
            this(id, keywords, advice, keywords.stream()
                    .map(k -> Pattern.compile("\\b" + Pattern.quote(k)))
                    .toList());
        }

        long score(String question) {
            return patterns.stream().filter(p -> p.matcher(question).find()).count();
        }
    }

    static final List<Topic> TOPICS = List.of(
            new Topic(
                    "heap",
                    List.of("outofmemory", "out of memory", "oom", "heap", "memory leak", "leak",
                            "metaspace", "memoria", "fuga"),
                    """
                    ### Heap and OutOfMemoryError
                    1. Read the exact message: `Java heap space` (heap full), `GC overhead limit \
                    exceeded` (GC thrashing), `Metaspace` (class loading, often redeploys or \
                    dynamic proxies), `unable to create native thread` (thread or OS limit, not \
                    heap), `Direct buffer memory` (off-heap NIO/Netty buffers).
                    2. Capture evidence: start with `-XX:+HeapDumpOnOutOfMemoryError \
                    -XX:HeapDumpPath=/dumps`, or dump a live process with `jcmd <pid> \
                    GC.heap_dump /tmp/heap.hprof`.
                    3. Open the dump in Eclipse MAT: the Leak Suspects report and the dominator \
                    tree show what retains memory. Usual culprits: unbounded caches or maps, \
                    static collections, ThreadLocals on pooled threads, listeners never removed.
                    4. Heap that keeps growing after full GCs (see GC logs) is a leak; a heap that \
                    is simply too small plateaus at the limit under load. Fix leaks before \
                    raising `-Xmx`."""),
            new Topic(
                    "containers",
                    List.of("container", "docker", "kubernetes", "k8s", "pod", "oomkilled",
                            "memory limit", "exit code 137", "cgroup", "contenedor"),
                    """
                    ### JVM in containers
                    1. Size the heap relative to the container limit with \
                    `-XX:MaxRAMPercentage=75` instead of a fixed `-Xmx`. The JVM reads cgroup \
                    limits (cgroup v2 since JDK 15, backported to 11.0.16 and 8u372).
                    2. Leave headroom: metaspace, thread stacks (about 1 MB each), code cache, GC \
                    structures and direct buffers live outside the heap. A heap as large as the \
                    limit gets the pod OOMKilled.
                    3. `OOMKilled` (exit code 137) is the kernel killing the container for \
                    exceeding its limit, not a Java `OutOfMemoryError`. Measure non-heap memory \
                    with `-XX:NativeMemoryTracking=summary` and `jcmd <pid> \
                    VM.native_memory summary`.
                    4. Set memory requests equal to limits for predictable sizing. With fewer than \
                    2 CPUs or less than 1792 MB the JVM falls back to SerialGC, so give \
                    latency-sensitive services at least 2 CPUs."""),
            new Topic(
                    "gc",
                    List.of("gc", "garbage", "pause", "latency", "g1", "zgc", "shenandoah",
                            "stop the world", "stop-the-world", "recolector", "pausa"),
                    """
                    ### Garbage collection
                    1. Turn on GC logging first: \
                    `-Xlog:gc*:file=gc.log:time,uptime,level,tags:filecount=5,filesize=20m`, then \
                    look at pause times, frequency and heap occupancy after GC.
                    2. G1 (the default) aims for `-XX:MaxGCPauseMillis=200`; lower it moderately \
                    and avoid fixing the young generation size (`-Xmn`), which disables its \
                    adaptive sizing.
                    3. For sub-millisecond pauses on large heaps use ZGC: `-XX:+UseZGC` \
                    (generational by default since JDK 23; on JDK 21 add \
                    `-XX:+ZGenerational`). Throughput-oriented batch jobs can use \
                    `-XX:+UseParallelGC`.
                    4. Frequent collections usually mean a high allocation rate: profile \
                    allocations with JFR (`jcmd <pid> JFR.start duration=60s \
                    filename=rec.jfr`) before tuning flags."""),
            new Topic(
                    "startup",
                    List.of("startup", "start up", "start-up", "cold start", "slow to start",
                            "boot time", "arranque", "tarda en iniciar", "tarda en arrancar"),
                    """
                    ### Startup time
                    1. Class Data Sharing: record an AppCDS archive with \
                    `-XX:ArchiveClassesAtExit=app.jsa` on a training run, then start with \
                    `-XX:SharedArchiveFile=app.jsa`. JDK 24+ adds the AOT cache (JEP 483), which \
                    also preloads and links classes.
                    2. Spring Boot: use lazy initialization where acceptable \
                    (`spring.main.lazy-initialization=true`), trim auto-configuration and \
                    consider Spring AOT processing.
                    3. For near-instant startup: GraalVM native image (trade-offs: build time, \
                    reflection configuration, lower peak throughput without PGO) or CRaC \
                    checkpoint/restore on JDKs that support it.
                    4. In containers, too little CPU at startup slows class loading and JIT \
                    compilation sharply; a higher CPU limit (or startup CPU boost) helps."""),
            new Topic(
                    "threads",
                    List.of("thread", "virtual thread", "concurrency", "deadlock", "blocked",
                            "thread pool", "loom", "synchronized", "hilo", "concurrencia"),
                    """
                    ### Threads and concurrency
                    1. Take thread dumps while the problem happens: `jcmd <pid> Thread.print`, \
                    three dumps a few seconds apart show what is stuck. Deadlocks are reported \
                    at the end of the dump.
                    2. For blocking, I/O-heavy services on Java 21+, virtual threads remove the \
                    thread-per-request ceiling: `spring.threads.virtual.enabled=true` in Spring \
                    Boot 3.2+.
                    3. Watch for pinning: before JDK 24 (JEP 491) a virtual thread blocking inside \
                    `synchronized` pins its carrier thread; use `ReentrantLock` on hot paths or \
                    upgrade.
                    4. Size bounded pools for I/O-bound work against the downstream limit (for \
                    example the database connection pool), not the CPU count."""),
            new Topic(
                    "cpu",
                    List.of("cpu", "profil", "hotspot", "flame", "throughput", "slow", "lento",
                            "rendimiento"),
                    """
                    ### CPU and profiling
                    1. Record with Java Flight Recorder, which is low-overhead and safe in \
                    production: `jcmd <pid> JFR.start duration=120s filename=cpu.jfr`, then open \
                    it in JDK Mission Control.
                    2. async-profiler produces flame graphs for CPU, allocations and locks: \
                    `asprof -d 60 -f flame.html <pid>`.
                    3. Check the GC share first (GC logs): high CPU with frequent collections is \
                    an allocation problem, not a code hotspot.
                    4. Benchmark micro-optimisations with JMH; ad-hoc timing loops mislead \
                    because of JIT warm-up."""));

    static final String GENERAL = """
            I specialise in JVM performance: memory and OutOfMemoryError, garbage collection, \
            JVM sizing in containers and Kubernetes, startup time, threads and CPU profiling.
            To give a specific diagnosis, please share: JDK version and vendor, JVM flags, \
            container memory and CPU limits, the exact error or symptom, and GC logs or a JFR \
            recording if you have them.""";

    static final String FOOTER = "_Rule-based answer. Share the JDK version, JVM flags and "
            + "container limits for a more specific diagnosis._";

    /** The playbook sections that match the question, best match first (empty if none). */
    public List<Topic> match(String question) {
        String q = question.toLowerCase(Locale.ROOT);
        return TOPICS.stream()
                .filter(t -> t.score(q) > 0)
                .sorted(Comparator.comparingLong((Topic t) -> t.score(q)).reversed())
                .limit(MAX_TOPICS)
                .toList();
    }

    /** Reference notes for the matched topics, used to ground the LLM mode. */
    public String notes(String question) {
        return match(question).stream().map(Topic::advice).collect(Collectors.joining("\n\n"));
    }

    @Override
    public Answer answer(String question, String contextId) {
        List<Topic> topics = match(question);
        String body = topics.isEmpty()
                ? GENERAL
                : topics.stream().map(Topic::advice).collect(Collectors.joining("\n\n"));
        return new Answer("## JVM Performance Specialist\n\n" + body + "\n\n" + FOOTER, "rules");
    }
}
