package dev.agency.jvmagent;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

/**
 * JVM Performance Specialist: a remote agent for the Agency Orchestrator.
 *
 * <p>It publishes an A2A Agent Card and answers JSON-RPC {@code message/send}. The orchestrator
 * discovers it at startup, indexes its skills and routes JVM questions to it like any local
 * specialist.
 */
@SpringBootApplication
@ConfigurationPropertiesScan
public class JvmSpecialistApplication {

    public static void main(String[] args) {
        SpringApplication.run(JvmSpecialistApplication.class, args);
    }
}
