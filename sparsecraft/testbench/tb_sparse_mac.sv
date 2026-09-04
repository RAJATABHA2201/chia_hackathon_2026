module tb_sparse_mac;

    // ------------------------------------------------
    // Signals
    // ------------------------------------------------

    logic clk;
    logic rst;
    logic start;

    logic signed [7:0] value0;
    logic signed [7:0] value1;

    logic [1:0] index0;
    logic [1:0] index1;

    logic signed [7:0] b0;
    logic signed [7:0] b1;
    logic signed [7:0] b2;
    logic signed [7:0] b3;

    logic signed [31:0] result;
    logic done;

    integer cycle_count;


    // ------------------------------------------------
    // Instantiate Sparse MAC
    // ------------------------------------------------

    sparse_mac dut (
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


    // ------------------------------------------------
    // Clock
    // ------------------------------------------------

    always #5 clk = ~clk;


    // ------------------------------------------------
    // Test
    // ------------------------------------------------

    initial begin

        // Initial values
        clk = 0;
        rst = 1;
        start = 0;

        value0 = 0;
        value1 = 0;

        index0 = 0;
        index1 = 0;

        b0 = 0;
        b1 = 0;
        b2 = 0;
        b3 = 0;

        cycle_count = 0;


        // ------------------------------------------------
        // Reset
        // ------------------------------------------------

        #10;
        rst = 0;


        // ------------------------------------------------
        // Test case
        //
        // Dense A:
        //
        // [ 3   0   0   7 ]
        //
        // 2:4 sparse representation:
        //
        // values = [3, 7]
        // indices = [0, 3]
        //
        // B:
        //
        // [10, 20, 30, 40]
        //
        // Expected:
        //
        // 3*10 + 7*40
        // = 30 + 280
        // = 310
        // ------------------------------------------------

        value0 = 3;
        value1 = 7;

        index0 = 0;
        index1 = 3;

        b0 = 10;
        b1 = 20;
        b2 = 30;
        b3 = 40;


        // ------------------------------------------------
        // Start computation
        // ------------------------------------------------

        #5;

        start = 1;

        cycle_count = 0;


        // Wait until hardware says computation is complete
        while (!done) begin
            @(posedge clk);
            cycle_count = cycle_count + 1;
        end


        start = 0;


        // ------------------------------------------------
        // Results
        // ------------------------------------------------

        $display("");
        $display("========================================");
        $display("       SparseCraft 2:4 Sparse MAC");
        $display("========================================");

        $display("Sparse values : [%0d, %0d]", value0, value1);
        $display("Indices       : [%0d, %0d]", index0, index1);

        $display("B             : [%0d, %0d, %0d, %0d]",
                 b0, b1, b2, b3);

        $display("----------------------------------------");

        $display("Result        = %0d", result);
        $display("Expected      = 310");
        $display("Cycles        = %0d", cycle_count);

        $display("----------------------------------------");


        // ------------------------------------------------
        // Correctness check
        // ------------------------------------------------

        if (result == 310)
            $display("STATUS        = PASS");
        else
            $display("STATUS        = FAIL");


        $display("========================================");
        $display("");


        // Give simulator time to finish
        #10;

        $finish;

    end

endmodule