`ifndef NUM_PE
    `define NUM_PE 2
`endif

module tb_sparse_array;

    parameter NUM_PE = `NUM_PE;
    parameter NUM_GROUPS = 8;

    logic clk;
    logic rst;
    logic start;

    logic signed [7:0] value0 [NUM_GROUPS];
    logic signed [7:0] value1 [NUM_GROUPS];

    logic [1:0] index0 [NUM_GROUPS];
    logic [1:0] index1 [NUM_GROUPS];

    logic signed [7:0] b0 [NUM_GROUPS];
    logic signed [7:0] b1 [NUM_GROUPS];
    logic signed [7:0] b2 [NUM_GROUPS];
    logic signed [7:0] b3 [NUM_GROUPS];

    logic signed [31:0] result;
    logic done;


    // ============================================================
    // DUT
    // ============================================================

    sparse_array #(
        .DATA_WIDTH(8),
        .OUT_WIDTH(32),
        .NUM_PE(NUM_PE),
        .NUM_GROUPS(NUM_GROUPS)
    ) dut (
        .clk(clk),
        .rst(rst),
        .start(start),

        .value0(value0),
        .value1(value1),

        .index0(index0),
        .index1(index1),

        .b0(b0),
        .b1(b1),
        .b2(b2),
        .b3(b3),

        .result(result),
        .done(done)
    );


    // ============================================================
    // Clock
    // ============================================================

    always #5 clk = ~clk;


    // ============================================================
    // Test
    // ============================================================

    initial begin

        integer i;
        integer cycles;
        integer timeout;

        clk = 0;
        rst = 1;
        start = 0;

        cycles = 0;
        timeout = 0;


        // --------------------------------------------------------
        // Initialize arrays
        // --------------------------------------------------------

        for (i = 0; i < NUM_GROUPS; i = i + 1) begin

            value0[i] = 0;
            value1[i] = 0;

            index0[i] = 0;
            index1[i] = 0;

            b0[i] = 0;
            b1[i] = 0;
            b2[i] = 0;
            b3[i] = 0;

        end


        // --------------------------------------------------------
        // Reset
        // --------------------------------------------------------

        repeat (2) @(posedge clk);

        rst = 0;


        // ========================================================
        // Sparse workload
        // ========================================================

        // Group 0: 3*10 + 7*40 = 310
        value0[0] = 3;
        value1[0] = 7;
        index0[0] = 0;
        index1[0] = 3;
        b0[0] = 10;
        b3[0] = 40;


        // Group 1: 2*1 + 5*2 = 12
        value0[1] = 2;
        value1[1] = 5;
        index0[1] = 0;
        index1[1] = 1;
        b0[1] = 1;
        b1[1] = 2;


        // Group 2: 6*2 + 8*4 = 44
        value0[2] = 6;
        value1[2] = 8;
        index0[2] = 0;
        index1[2] = 2;
        b0[2] = 2;
        b2[2] = 4;


        // Group 3: 9*2 + 4*4 = 34
        value0[3] = 9;
        value1[3] = 4;
        index0[3] = 1;
        index1[3] = 3;
        b1[3] = 2;
        b3[3] = 4;


        // Group 4: 1*5 + 3*6 = 23
        value0[4] = 1;
        value1[4] = 3;
        index0[4] = 0;
        index1[4] = 2;
        b0[4] = 5;
        b2[4] = 6;


        // Group 5: 4*2 + 2*7 = 22
        value0[5] = 4;
        value1[5] = 2;
        index0[5] = 1;
        index1[5] = 3;
        b1[5] = 2;
        b3[5] = 7;


        // Group 6: 5*3 + 6*4 = 39
        value0[6] = 5;
        value1[6] = 6;
        index0[6] = 0;
        index1[6] = 2;
        b0[6] = 3;
        b2[6] = 4;


        // Group 7: 7*2 + 1*5 = 19
        value0[7] = 7;
        value1[7] = 1;
        index0[7] = 1;
        index1[7] = 3;
        b1[7] = 2;
        b3[7] = 5;


        // ========================================================
        // Start computation
        // ========================================================

        // Wait for falling edge so start is not racing with clock
        @(negedge clk);

        start = 1;

        // Hold start for one complete clock cycle
        @(negedge clk);

        start = 0;


        // ========================================================
        // Wait for DONE
        // ========================================================

        cycles = 0;

        while (!done && cycles < 100) begin

            @(posedge clk);

            cycles = cycles + 1;

        end


        // ========================================================
        // Results
        // ========================================================

        $display("");
        $display("========================================");
        $display("      SparseCraft PE Array");
        $display("========================================");

        $display("NUM_PE      = %0d", NUM_PE);
        $display("NUM_GROUPS  = %0d", NUM_GROUPS);

        $display("----------------------------------------");

        $display("Cycles      = %0d", cycles);
        $display("Result      = %0d", result);
        $display("Expected    = 503");

        $display("----------------------------------------");


        if (cycles >= 100) begin

            $display("STATUS      = TIMEOUT");
            $display("ERROR       = Hardware never asserted DONE");

        end

        else if (result == 503) begin

            $display("STATUS      = PASS");

        end

        else begin

            $display("STATUS      = FAIL");

        end


        $display("========================================");
        $display("");

        $finish;

    end

endmodule