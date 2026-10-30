FROM maven:3.9.11-eclipse-temurin-21 AS authorizer
WORKDIR /build
COPY images/kafka/authorizer/pom.xml ./
COPY images/kafka/authorizer/src ./src
RUN mvn -B package

FROM quay.io/strimzi/kafka:1.2.0-kafka-4.3.1
USER root
COPY --from=authorizer /build/target/fdp-kafka-authorizer-0.1.0.jar /opt/kafka/libs/
USER 1001
