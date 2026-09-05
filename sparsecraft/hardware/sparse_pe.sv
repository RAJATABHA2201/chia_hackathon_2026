module sparse_pe #(
    parameter DATA_WIDTH = 8,
    parameter OUT_WIDTH  = 32,
    parameter NUM_GROUPS = 4
)(
    input  logic clk,
    input  logic rst,
    input  logic start,

    // Sparse values
    input logic signed [DATA_WIDTH-1:0] value0 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] value1 [NUM_GROUPS],

    // Indices of non-zero values
    input logic [1:0] index0 [NUM_GROUPS],
    input logic [1:0] index1 [NUM_GROUPS],

    // Dense input vector for each group
    input logic signed [DATA_WIDTH-1:0] b0 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] b1 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] b2 [NUM_GROUPS],
    input logic signed [DATA_WIDTH-1:0] b3 [NUM_GROUPS],

    output logic signed [OUT_WIDTH-1:0] result,
    output logic done
);

    integer i;

    logic signed [OUT_WIDTH-1:0] accumulator;

    logic signed [DATA_WIDTH-1:0] selected_b0;
    logic signed [DATA_WIDTH-1:0] selected_b1;

    logic signed [OUT_WIDTH-1:0] group_result;


    // ------------------------------------------------
    // Compute one sparse group at a time
    // ------------------------------------------------

    always_comb begin

        group_result = '0;

        for (i = 0; i < NUM_GROUPS; i = i + 1) begin

            case (index0[i])
                2'd0: selected_b0 = b0[i];
                2'd1: selected_b0 = b1[i];
                2'd2: selected_b0 = b2[i];
                2'd3: selected_b0 = b3[i];
            endcase

            case (index1[i])
                2'd0: selected_b1 = b0[i];
                2'd1: selected_b1 = b1[i];
                2'd2: selected_b1 = b2[i];
                2'd3: selected_b1 = b3[i];
            endcase

            group_result =
                group_result +
                value0[i] * selected_b0 +
                value1[i] * selected_b1;

        end

    end


    // ------------------------------------------------
    // Accumulator
    // ------------------------------------------------

    always_ff @(posedge clk) begin

        if (rst) begin

            accumulator <= '0;
            result      <= '0;
            done        <= 1'b0;

        end
        else begin

            done <= 1'b0;

            if (start) begin

                accumulator <= group_result;
                result      <= group_result;

                done <= 1'b1;

            end

        end

    end

endmodule