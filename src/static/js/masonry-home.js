(function ($) {
    var $container = $('.portfolio');

    function colWidth() {
        var w = $container.width();
        var columnNum = 1;
        var columnWidth = 50;

        if (w > 1200) {
            columnNum = 5;
        } else if (w > 900) {
            columnNum = 3;
        } else if (w > 600) {
            columnNum = 2;
        } else if (w > 300) {
            columnNum = 1;
        }

        columnWidth = Math.floor(w / columnNum);

        $container.find('.pitem').each(function () {
            var $item = $(this);
            var multiplier_w = $item.attr('class').match(/item-w(\d)/);
            var multiplier_h = $item.attr('class').match(/item-h(\d)/);
            var width = multiplier_w ? columnWidth * multiplier_w[1] - 0 : columnWidth - 5;
            var height = multiplier_h ? columnWidth * multiplier_h[1] * 1 - 5 : columnWidth * 0.5 - 5;

            $item.css({
                width: width,
                height: height
            });
        });

        return columnWidth;
    }

    function refreshWaypoints() {
        setTimeout(function () {
            // You can add waypoint refreshing logic here if needed.
        }, 3000);
    }

    $('nav.portfolio-filter ul a').on('click', function (e) {
        e.preventDefault(); // Prevent default anchor behavior
        var selector = $(this).attr('data-filter');

        $container.isotope({ filter: selector });
        refreshWaypoints(); // Call refreshWaypoints after filtering

        $('nav.portfolio-filter ul a').removeClass('active');
        $(this).addClass('active');

        
    });

    function setPortfolio() {
        colWidth(); // Call colWidth to recalculate and set item sizes
        $container.isotope('layout'); // Use 'layout' instead of 'reLayout' in Isotope v3
    }

    $container.imagesLoaded(function () {
        $container.isotope({
            itemSelector: '.pitem',
            layoutMode: 'masonry',
            masonry: {
                columnWidth: colWidth(),
                gutter: 10
            }
        });
    });

    $(window).on('resize', function() {
        // Set portfolio on resize.
        setPortfolio();
    });
    
}(jQuery));
