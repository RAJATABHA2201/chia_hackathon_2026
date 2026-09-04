module sparse_mac #(
    parameter DATA_WIDTH = 8,
    parameter OUT_WIDTH  = 32
)(
    input  logic clk,
    input  logic rst,
    input  logic start,

    input  logic signed [DATA_WIDTH-1:0] value0,
    input  logic signed [DATA_WIDTH-1:0] value1,

    input  logic [1:0] index0,
    input  logic [1:0] index1,

    input  logic signed [DATA_WIDTH-1:0] b0,
    input  logic signed [DATA_WIDTH-1:0] b1,
    input  logic signed [DATA_WIDTH-1:0] b2,
    input  logic signed [DATA_WIDTH-1:0] b3,

    output logic signed [OUT_WIDTH-1:0] result,
    output logic done
);

    logic signed [DATA_WIDTH-1:0] selected_b0;
    logic signed [DATA_WIDTH-1:0] selected_b1;

    // 2:4 sparse decoder
    always_comb begin
        case (index0)
            2'd0: selected_b0 = b0;
            2'd1: selected_b0 = b1;
            2'd2: selected_b0 = b2;
            2'd3: selected_b0 = b3;
        endcase

        case (index1)
            2'd0: selected_b1 = b0;
            2'd1: selected_b1 = b1;
            2'd2: selected_b1 = b2;
            2'd3: selected_b1 = b3;
        endcase
    end

    // Sparse MAC
    always_ff @(posedge clk) begin
        if (rst) begin
            result <= '0;
            done   <= 1'b0;
        end
        else begin
            done <= 1'b0;

            if (start) begin
                result <= value0 * selected_b0 +
                          value1 * selected_b1;

                done <= 1'b1;
            end
        end
    end

endmodule