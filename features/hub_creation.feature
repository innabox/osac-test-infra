Feature: Hub Creation
    As an OSAC operator
    I want to create and register hubs via the fulfillment service
    So that I can manage hub resources in the OSAC system

    Background:
        Given the fulfillment service is accessible
        And I am authenticated with the fulfillment CLI

    Scenario: Create a hub with a unique ID
        When I create a hub with a unique ID
        Then the hub should be registered in the fulfillment service
        And the hub details should be retrievable via gRPC

    Scenario: Create a hub with a specific ID
        When I create a hub with ID "e2e-test-hub-bdd-001"
        Then the hub should be registered in the fulfillment service
        And the hub ID should be "e2e-test-hub-bdd-001"
