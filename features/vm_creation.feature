Feature: VM Creation
    As an OSAC operator
    I want to create virtual machines with various configurations
    So that I can provision VMs for different workloads

    Background:
        Given the fulfillment service is accessible
        And I am authenticated with the fulfillment CLI
        And a hub is available
        And the VM template exists

    Scenario: Create a VM with default parameters
        When I create a VM with template "osac.templates.ocp_virt_vm"
        Then the VM should be registered in the fulfillment service
        And the VM should reach ready state within 10 minutes

    Scenario Outline: Create a VM with <variant> configuration
        When I create a VM with template "osac.templates.ocp_virt_vm" and parameters:
            | name   | value   |
            | <p1>   | <v1>    |
            | <p2>   | <v2>    |
        Then the VM should be registered in the fulfillment service
        And the VM should reach ready state within 10 minutes

        Examples: CPU and Memory
            | variant    | p1        | v1 | p2     | v2  |
            | cpu_memory | cpu_cores | 4  | memory | 4Gi |

        Examples: Custom Disk
            | variant     | p1        | v1 | p2        | v2   |
            | custom_disk | cpu_cores | 2  | disk_size | 20Gi |

    Scenario: Create a VM with SSH public key and verify access
        When I create a VM with template "osac.templates.ocp_virt_vm" and generated SSH key
        Then the VM should be registered in the fulfillment service
        And the VM should reach ready state within 10 minutes
        And I should be able to SSH into the VM

    Scenario: Create a VM with cloud-init configuration and verify access
        When I create a VM with template "osac.templates.ocp_virt_vm" and cloud-init with SSH key
        Then the VM should be registered in the fulfillment service
        And the VM should reach ready state within 10 minutes
        And I should be able to SSH into the VM
