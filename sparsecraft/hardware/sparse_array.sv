`ifndef NUM_PE
    `define NUM_PE 2
`endif

module sparse_array #(
    parameter DATA_WIDTH = 8,
    parameter OUT_WIDTH  = 32,
    parameter NUM_PE     = `NUM_PE,
    parameter NUM_GROUPS = 8
)(
    input  logic clk,
    input  logic rst,
    input  logic start,

    input logic signed [DATA_WIDTH-1:0] value0 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] value1 [NUM_GROUPS],

    input logic [1:0] index0 [NUM_GROUPS],
    input logic [1:0] index1 [NUM_GROUPS],

    input logic signed [DATA_WIDTH-1:0] b0 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] b1 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] b2 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] b3 [NUM_GROUPS],

    output logic signed [OUT_WIDTH-1:0] result,
    output logic done
);

    integer i;
    integer current_group;

    logic busy;

    logic signed [OUT_WIDTH-1:0] accumulator;

    logic signed [OUT_WIDTH-1:0] cycle_sum;

    logic signed [DATA_WIDTH-1:0] selected_value0;
    logic signed [DATA_WIDTH-1:0] selected_value1;

    // Number of groups processed so far
    integer groups_processed;


    // ============================================================
    // COMBINATIONAL PE COMPUTATION
    //
    // In one clock cycle, NUM_PE groups are evaluated in parallel.
    //
    // NUM_PE = 1:
    //   1 group/cycle
    //
    // NUM_PE = 2:
    //   2 groups/cycle
    //
    // NUM_PE = 4:
    //   4 groups/cycle
    //
    // ============================================================

    always_comb begin

        cycle_sum = '0;

        selected_value0 = '0;
        selected_value1 = '0;

        for (i = 0; i < NUM_PE; i = i + 1) begin

            current_group = groups_processed + i;

            if (current_group < NUM_GROUPS) begin

                // ------------------------------------------------
                // Select B for first non-zero value
                // ------------------------------------------------

                case (index0[current_group])
                    2'd0: selected_value0 = b0[current_group];
                    2'd1: selected_value0 = b1[current_group];
                    2'd2: selected_value0 = b2[current_group];
                    2'd3: selected_value0 = b3[current_group];
                    default: selected_value0 = '0;
                endcase


                // ------------------------------------------------
                // Select B for second non-zero value
                // ------------------------------------------------

                case (index1[current_group])
                    2'd0: selected_value1 = b0[current_group];
                    2'd1: selected_value1 = b1[current_group];
                    2'd2: selected_value1 = b2[current_group];
                    2'd3: selected_value1 = b3[current_group];
                    default: selected_value1 = '0;
                endcase


                // ------------------------------------------------
                // 2:4 sparse MAC
                // ------------------------------------------------

                cycle_sum =
                    cycle_sum
                    + ($signed(value0[current_group])
                       * $signed(selected_value0))
                    + ($signed(value1[current_group])
                       * $signed(selected_value1));

            end

        end

    end


    // ============================================================
    // SEQUENTIAL CONTROLLER
    // ============================================================

    always_ff @(posedge clk) begin

        if (rst) begin

            result           <= '0;
            accumulator      <= '0;
            groups_processed <= 0;
            busy             <= 1'b0;
            done             <= 1'b0;

        end

        else begin

            // done is a one-cycle pulse
            done <= 1'b0;


            // ------------------------------------------------
            // Start computation
            // ------------------------------------------------

            if (start && !busy) begin

                accumulator      <= '0;
                groups_processed <= 0;
                busy             <= 1'b1;

            end


            // ------------------------------------------------
            // Process NUM_PE groups during this cycle
            // ------------------------------------------------

            else if (busy) begin

                accumulator <= accumulator + cycle_sum;

                groups_processed <=
                    groups_processed + NUM_PE;


                // ------------------------------------------------
                // Last group batch
                // ------------------------------------------------

                if (groups_processed + NUM_PE >= NUM_GROUPS) begin

                    result <= accumulator + cycle_sum;

                    done <= 1'b1;

                    busy <= 1'b0;

                end

            end

        end

    end

endmodule