package dev.agency.jvmagent.specialist;

/** Answers one user turn of a conversation identified by the A2A {@code contextId}. */
public interface Specialist {

    Answer answer(String question, String contextId);

    /**
     * @param mode how the answer was produced: {@code rules}, {@code llm}, or {@code
     *     rules-fallback} when the LLM failed and the rule engine answered instead
     */
    record Answer(String text, String mode) {}
}
