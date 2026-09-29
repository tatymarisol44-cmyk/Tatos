package dev.agency.jvmagent.specialist;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

class RuleBasedSpecialistTest {

    private final RuleBasedSpecialist rules = new RuleBasedSpecialist();

    @ParameterizedTest
    @CsvSource(delimiter = '|', value = {
        "We get java.lang.OutOfMemoryError: Java heap space every night | heap",
        "Our pods are OOMKilled with exit code 137 in Kubernetes | containers",
        "G1 GC pauses of 2 seconds hurt our latency | gc",
        "The Spring Boot service has a slow cold start | startup",
        "Requests hang, I suspect a deadlock between threads | threads",
        "CPU is at 100% and I want a flame graph from a profiler | cpu",
        "¿Cómo reduzco las pausas del recolector? | gc",
    })
    void matchesTheRightPlaybook(String question, String topic) {
        assertThat(rules.match(question)).extracting(RuleBasedSpecialist.Topic::id)
                .first().isEqualTo(topic);
    }

    @Test
    void tiedTopicsAreBothIncluded() {
        // "contenedor" (containers) and "memoria" (heap) score the same: answer both.
        assertThat(rules.match("Mi contenedor se queda sin memoria"))
                .extracting(RuleBasedSpecialist.Topic::id)
                .containsExactlyInAnyOrder("heap", "containers");
    }

    @Test
    void keywordsMatchAtWordStartsOnly() {
        // "oom" must not match "room"/"zoom", but "profil" matches "profiler".
        assertThat(rules.match("book a zoom meeting room")).isEmpty();
        assertThat(rules.match("which profiler?")).extracting(RuleBasedSpecialist.Topic::id)
                .containsExactly("cpu");
    }

    @Test
    void combinesUpToThreeTopicsBestFirst() {
        var topics = rules.match(
                "Heap OOM in a Kubernetes pod with long GC pauses, slow startup and deadlocked "
                        + "threads burning CPU");
        assertThat(topics).hasSize(RuleBasedSpecialist.MAX_TOPICS);
    }

    @Test
    void answerIncludesConcreteCommands() {
        var answer = rules.answer("OutOfMemoryError in production", "ctx");
        assertThat(answer.mode()).isEqualTo("rules");
        assertThat(answer.text())
                .startsWith("## JVM Performance Specialist")
                .contains("-XX:+HeapDumpOnOutOfMemoryError", "jcmd <pid> GC.heap_dump")
                .endsWith(RuleBasedSpecialist.FOOTER);
    }

    @Test
    void unknownQuestionsGetTheTriageChecklist() {
        var answer = rules.answer("hello there", "ctx");
        assertThat(answer.text()).contains("please share: JDK version");
        assertThat(rules.notes("hello there")).isEmpty();
    }
}
