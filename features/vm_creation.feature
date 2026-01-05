Feature: VM Creation
    As an OSAC operator
    I want to create and manage virtual machines via the fulfillment service
    So that I can provision VMs for workloads

    Background:
        Given the fulfillment service is accessible
        And I am authenticated with the fulfillment CLI
        And a hub is available
        And the VM template exists

    Scenario: Create a VM with the standard template
        When I create a VM with template "osac.templates.ocp_virt_vm"
        Then the VM should be registered in the fulfillment service
        And the VM should reach ready state within 10 minutes
